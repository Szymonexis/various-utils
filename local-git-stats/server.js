#!/usr/bin/env node
'use strict';

/**
 * local-git-stats
 *
 * A tiny, dependency-free Node app that visualises statistics for a local git
 * repository: commits per contributor, a commit graph/tree, activity over time
 * and more. It shells out to the local `git` binary under the hood.
 *
 * Run:  node server.js  [--port 4321]  [--open]
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');
const { execFile } = require('child_process');
const os = require('os');

const PUBLIC_DIR = path.join(__dirname, 'public');

// ---- argument parsing -------------------------------------------------------

function parseArgs(argv) {
	const args = { port: 4321, open: false };
	for (let i = 2; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--port' || a === '-p') {
			args.port = parseInt(argv[++i], 10) || args.port;
		} else if (a === '--open' || a === '-o') {
			args.open = true;
		} else if (a === '--help' || a === '-h') {
			console.log('Usage: node server.js [--port <n>] [--open]');
			process.exit(0);
		}
	}
	return args;
}

const ARGS = parseArgs(process.argv);

// ---- git helpers ------------------------------------------------------------

const MIME = {
	'.html': 'text/html; charset=utf-8',
	'.js': 'text/javascript; charset=utf-8',
	'.css': 'text/css; charset=utf-8',
	'.svg': 'image/svg+xml',
	'.ico': 'image/x-icon',
	'.json': 'application/json; charset=utf-8',
};

/**
 * Run a git command inside `repo`. Resolves with stdout (string).
 * Uses execFile (no shell) so repo paths / branch names cannot inject.
 */
function git(repo, gitArgs, { maxBuffer = 64 * 1024 * 1024 } = {}) {
	return new Promise((resolve, reject) => {
		execFile(
			'git',
			['-C', repo, ...gitArgs],
			{ maxBuffer, windowsHide: true },
			(err, stdout, stderr) => {
				if (err) {
					err.stderr = stderr;
					return reject(err);
				}
				resolve(stdout);
			}
		);
	});
}

/** Expand a leading ~ and resolve to an absolute path. */
function resolveRepoPath(p) {
	if (!p) return p;
	let out = p.trim();
	if (out === '~') out = os.homedir();
	else if (out.startsWith('~/')) out = path.join(os.homedir(), out.slice(2));
	return path.resolve(out);
}

async function validateRepo(repo) {
	const top = (await git(repo, ['rev-parse', '--show-toplevel'])).trim();
	return top;
}

// Unit separator + record separator make parsing log output unambiguous.
const FS = '\x1f';
const RS = '\x1e';

async function getBranches(repo) {
	const out = await git(repo, [
		'for-each-ref',
		'--sort=-committerdate',
		`--format=%(refname:short)${FS}%(objectname:short)${FS}%(committerdate:iso8601)${FS}%(HEAD)`,
		'refs/heads',
		'refs/remotes',
	]);
	const branches = [];
	for (const line of out.split('\n')) {
		if (!line.trim()) continue;
		const [name, sha, date, head] = line.split(FS);
		if (name.endsWith('/HEAD')) continue; // skip symbolic origin/HEAD
		branches.push({
			name,
			sha,
			date,
			current: head === '*',
			remote: name.includes('/') && !name.startsWith('refs/'),
		});
	}
	let current = '';
	try {
		current = (await git(repo, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
	} catch (_) {
		/* detached / empty repo */
	}
	return { branches, current };
}

/**
 * Build the commit graph data for a branch (capped for performance).
 */
async function getGraph(repo, branch, limit) {
	const fmt = [
		'%H', // full hash
		'%h', // short hash
		'%P', // parent hashes (space separated)
		'%an', // author name
		'%ae', // author email
		'%aI', // author date ISO
		'%s', // subject
		'%D', // ref names (decorations)
	].join(FS);

	const out = await git(repo, [
		'log',
		`--max-count=${limit}`,
		`--pretty=format:${fmt}${RS}`,
		branch,
	]);

	const commits = [];
	for (const record of out.split(RS)) {
		const line = record.replace(/^\n/, '');
		if (!line.trim()) continue;
		const [hash, short, parents, an, ae, aI, subject, refs] = line.split(FS);
		commits.push({
			hash,
			short,
			parents: parents ? parents.trim().split(/\s+/).filter(Boolean) : [],
			author: an,
			email: ae,
			date: aI,
			subject: subject || '',
			refs: refs ? refs.split(',').map((s) => s.trim()).filter(Boolean) : [],
		});
	}
	return commits;
}

/**
 * Overview statistics: contributor counts, line churn, activity buckets.
 */
async function getOverview(repo, branch) {
	// Contributor commit counts (fast).
	const shortlog = await git(repo, [
		'shortlog',
		'-sne',
		'--all',
		'--no-merges',
		branch,
		'--',
	]).catch(() => '');

	const contributors = {};
	for (const line of shortlog.split('\n')) {
		const m = line.match(/^\s*(\d+)\s+(.*?)\s+<(.*?)>\s*$/);
		if (!m) continue;
		const [, count, name, email] = m;
		contributors[email] = {
			name,
			email,
			commits: parseInt(count, 10),
			additions: 0,
			deletions: 0,
		};
	}

	// Totals + first/last commit + per-day / per-author churn via one log pass.
	// We cap numstat scanning to keep big repos snappy.
	const NUMSTAT_LIMIT = 4000;
	const log = await git(repo, [
		'log',
		`--max-count=${NUMSTAT_LIMIT}`,
		'--numstat',
		'--no-merges',
		`--pretty=format:${RS}%aI${FS}%ae${FS}%an`,
		branch,
	]).catch(() => '');

	const byDay = {};
	const byHour = new Array(24).fill(0);
	const byWeekday = new Array(7).fill(0);
	let scannedCommits = 0;
	let firstDate = null;
	let lastDate = null;
	let totalAdd = 0;
	let totalDel = 0;

	let curEmail = null;
	let curName = null;
	for (const block of log.split(RS)) {
		if (!block.trim()) continue;
		const lines = block.split('\n');
		const header = lines.shift();
		const [iso, email, name] = header.split(FS);
		curEmail = email;
		curName = name;
		scannedCommits++;

		const d = new Date(iso);
		if (!firstDate || d < firstDate) firstDate = d;
		if (!lastDate || d > lastDate) lastDate = d;
		const dayKey = iso.slice(0, 10);
		byDay[dayKey] = (byDay[dayKey] || 0) + 1;
		byHour[d.getHours()]++;
		byWeekday[d.getDay()]++;

		for (const l of lines) {
			if (!l.trim()) continue;
			const parts = l.split('\t');
			if (parts.length < 3) continue;
			const add = parts[0] === '-' ? 0 : parseInt(parts[0], 10) || 0;
			const del = parts[1] === '-' ? 0 : parseInt(parts[1], 10) || 0;
			totalAdd += add;
			totalDel += del;
			if (!contributors[curEmail]) {
				contributors[curEmail] = {
					name: curName,
					email: curEmail,
					commits: 0,
					additions: 0,
					deletions: 0,
				};
			}
			contributors[curEmail].additions += add;
			contributors[curEmail].deletions += del;
		}
	}

	// Total commit count on the branch (independent of numstat cap).
	let totalCommits = 0;
	try {
		totalCommits = parseInt(
			(await git(repo, ['rev-list', '--count', branch])).trim(),
			10
		);
	} catch (_) {
		totalCommits = scannedCommits;
	}

	const contributorList = Object.values(contributors).sort(
		(a, b) => b.commits - a.commits
	);

	return {
		totalCommits,
		scannedCommits,
		churnCapped: scannedCommits >= NUMSTAT_LIMIT,
		contributorCount: contributorList.length,
		contributors: contributorList,
		totalAdditions: totalAdd,
		totalDeletions: totalDel,
		firstCommit: firstDate ? firstDate.toISOString() : null,
		lastCommit: lastDate ? lastDate.toISOString() : null,
		activeDays: Object.keys(byDay).length,
		byDay,
		byHour,
		byWeekday,
	};
}

// ---- http server ------------------------------------------------------------

function sendJSON(res, status, body) {
	const payload = JSON.stringify(body);
	res.writeHead(status, {
		'Content-Type': 'application/json; charset=utf-8',
		'Content-Length': Buffer.byteLength(payload),
	});
	res.end(payload);
}

function sendError(res, status, message) {
	sendJSON(res, status, { error: message });
}

function serveStatic(res, pathname) {
	let filePath = pathname === '/' ? '/index.html' : pathname;
	// prevent path traversal
	const resolved = path.normalize(path.join(PUBLIC_DIR, filePath));
	if (!resolved.startsWith(PUBLIC_DIR)) {
		return sendError(res, 403, 'Forbidden');
	}
	fs.readFile(resolved, (err, data) => {
		if (err) {
			res.writeHead(404, { 'Content-Type': 'text/plain' });
			return res.end('Not found');
		}
		const ext = path.extname(resolved).toLowerCase();
		res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
		res.end(data);
	});
}

async function handleApi(req, res, parsed) {
	const q = parsed.query;
	const repoRaw = q.repo;
	const route = parsed.pathname;

	try {
		// /api/home — convenience: where the user's home is, for the picker hint.
		if (route === '/api/home') {
			return sendJSON(res, 200, { home: os.homedir(), cwd: process.cwd() });
		}

		if (!repoRaw) return sendError(res, 400, 'Missing "repo" query parameter.');
		const repo = resolveRepoPath(repoRaw);

		if (!fs.existsSync(repo)) {
			return sendError(res, 400, `Path does not exist: ${repo}`);
		}

		let topLevel;
		try {
			topLevel = await validateRepo(repo);
		} catch (e) {
			return sendError(
				res,
				400,
				`Not a git repository: ${repo}`
			);
		}

		if (route === '/api/validate') {
			const { branches, current } = await getBranches(repo);
			return sendJSON(res, 200, { ok: true, repo: topLevel, branches, current });
		}

		if (route === '/api/branches') {
			return sendJSON(res, 200, await getBranches(repo));
		}

		if (route === '/api/overview') {
			const branch = q.branch;
			if (!branch) return sendError(res, 400, 'Missing "branch".');
			return sendJSON(res, 200, await getOverview(repo, branch));
		}

		if (route === '/api/graph') {
			const branch = q.branch;
			if (!branch) return sendError(res, 400, 'Missing "branch".');
			const limit = Math.min(parseInt(q.limit, 10) || 200, 1000);
			const commits = await getGraph(repo, branch, limit);
			return sendJSON(res, 200, { commits, limit });
		}

		return sendError(res, 404, 'Unknown API route.');
	} catch (e) {
		return sendError(res, 500, (e && (e.stderr || e.message)) || 'Internal error');
	}
}

const server = http.createServer((req, res) => {
	const parsed = url.parse(req.url, true);
	if (parsed.pathname.startsWith('/api/')) {
		return handleApi(req, res, parsed);
	}
	return serveStatic(res, parsed.pathname);
});

server.listen(ARGS.port, () => {
	const link = `http://localhost:${ARGS.port}`;
	console.log(`\n  local-git-stats running at  ${link}\n`);
	if (ARGS.open) {
		const opener =
			process.platform === 'darwin'
				? 'open'
				: process.platform === 'win32'
				? 'start'
				: 'xdg-open';
		execFile(opener, [link], () => {});
	}
});
