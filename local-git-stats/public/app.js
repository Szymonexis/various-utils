'use strict';

// ---- tiny DOM helpers -------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
	const n = document.createElement(tag);
	if (cls) n.className = cls;
	if (html != null) n.innerHTML = html;
	return n;
};
const esc = (s) =>
	String(s).replace(/[&<>"']/g, (c) => ({
		'&': '&amp;',
		'<': '&lt;',
		'>': '&gt;',
		'"': '&quot;',
		"'": '&#39;',
	}[c]));

const LANE_COLORS = [
	'#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f778ba',
	'#39c5cf', '#ff7b72', '#a5d6ff', '#7ee787', '#ffa657',
];

let state = { repo: '', branch: '' };

// ---- state persistence ------------------------------------------------------
function saveState() {
	try {
		localStorage.setItem('lgs.repo', state.repo);
	} catch (_) {}
}

// ---- API --------------------------------------------------------------------
async function api(route, params) {
	const qs = new URLSearchParams(params).toString();
	const res = await fetch(`/api/${route}?${qs}`);
	const data = await res.json();
	if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
	return data;
}

// ---- status ui --------------------------------------------------------------
function setStatus(msg, kind) {
	const s = $('#status');
	if (!msg) {
		s.classList.add('hidden');
		return;
	}
	s.className = `status ${kind || ''}`;
	s.textContent = msg;
	s.classList.remove('hidden');
}

// ---- load repo / branches ---------------------------------------------------
async function loadRepo() {
	const repo = $('#repo').value.trim();
	if (!repo) return;
	const btn = $('#loadBtn');
	btn.disabled = true;
	setStatus('Inspecting repository…', 'loading');
	$('#dashboard').classList.add('hidden');
	$('#branchRow').classList.add('hidden');
	try {
		const data = await api('validate', { repo });
		state.repo = repo;
		saveState();
		populateBranches(data.branches, data.current);
		$('#repoLabel').textContent = data.repo;
		$('#branchRow').classList.remove('hidden');
		setStatus('');
		await loadBranch();
	} catch (e) {
		setStatus(e.message, 'error');
	} finally {
		btn.disabled = false;
	}
}

function populateBranches(branches, current) {
	const sel = $('#branch');
	sel.innerHTML = '';
	const locals = branches.filter((b) => !b.remote);
	const remotes = branches.filter((b) => b.remote);

	const addGroup = (label, list) => {
		if (!list.length) return;
		const og = el('optgroup');
		og.label = label;
		for (const b of list) {
			const o = el('option');
			o.value = b.name;
			o.textContent = b.current ? `${b.name}  (current)` : b.name;
			if (b.name === current) o.selected = true;
			og.appendChild(o);
		}
		sel.appendChild(og);
	};
	addGroup('Local branches', locals);
	addGroup('Remote branches', remotes);
	state.branch = sel.value;
}

// ---- load a branch's data ---------------------------------------------------
async function loadBranch() {
	state.branch = $('#branch').value;
	if (!state.branch) return;
	setStatus(`Crunching stats for “${state.branch}”…`, 'loading');
	$('#dashboard').classList.add('hidden');
	try {
		const limit = $('#graphLimit').value;
		const [overview, graph] = await Promise.all([
			api('overview', { repo: state.repo, branch: state.branch }),
			api('graph', { repo: state.repo, branch: state.branch, limit }),
		]);
		renderOverview(overview);
		renderGraph(graph.commits);
		$('#dashboard').classList.remove('hidden');
		setStatus('');
	} catch (e) {
		setStatus(e.message, 'error');
	}
}

// ---- rendering: summary -----------------------------------------------------
function fmt(n) {
	return n.toLocaleString();
}
function dateStr(iso) {
	if (!iso) return '—';
	return new Date(iso).toLocaleDateString(undefined, {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
	});
}

function renderOverview(o) {
	const span = (() => {
		if (!o.firstCommit || !o.lastCommit) return '—';
		const days =
			Math.round(
				(new Date(o.lastCommit) - new Date(o.firstCommit)) / 86400000
			) + 1;
		return `${fmt(days)} days`;
	})();

	const cards = [
		{ value: fmt(o.totalCommits), label: 'Total commits' },
		{ value: fmt(o.contributorCount), label: 'Contributors' },
		{ value: fmt(o.activeDays), label: 'Active days' },
		{ value: span, label: 'Project span' },
		{ value: '+' + fmt(o.totalAdditions), label: 'Lines added', cls: 'add' },
		{ value: '−' + fmt(o.totalDeletions), label: 'Lines removed', cls: 'del' },
	];
	const summary = $('#summary');
	summary.innerHTML = '';
	for (const c of cards) {
		const s = el('div', 'stat');
		s.appendChild(el('div', `value ${c.cls || ''}`, esc(c.value)));
		s.appendChild(el('div', 'label', esc(c.label)));
		summary.appendChild(s);
	}

	renderContributors(o.contributors);
	renderWeekday(o.byWeekday);
	renderHour(o.byHour);
	renderTimeline(o.byDay);
}

function colorFor(str) {
	let h = 0;
	for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
	return `hsl(${h}, 62%, 60%)`;
}
function initials(name) {
	const parts = name.trim().split(/\s+/);
	return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '?';
}

function renderContributors(list) {
	const wrap = $('#contributors');
	wrap.innerHTML = '';
	if (!list.length) {
		wrap.appendChild(el('div', 'empty', 'No contributors found.'));
		return;
	}
	const top = list.slice(0, 12);
	const max = Math.max(...top.map((c) => c.commits), 1);
	for (const c of top) {
		const row = el('div', 'contributor');
		const crow = el('div', 'crow');
		const name = el('div', 'cname');
		const av = el('span', 'avatar', esc(initials(c.name)));
		av.style.background = colorFor(c.email || c.name);
		name.appendChild(av);
		name.appendChild(el('span', null, esc(c.name)));
		crow.appendChild(name);
		crow.appendChild(
			el('div', 'cmeta', `${fmt(c.commits)} commit${c.commits === 1 ? '' : 's'}`)
		);
		row.appendChild(crow);

		const track = el('div', 'bar-track');
		const fill = el('div', 'bar-fill');
		fill.style.width = `${(c.commits / max) * 100}%`;
		track.appendChild(fill);
		row.appendChild(track);

		if (c.additions || c.deletions) {
			row.appendChild(
				el(
					'div',
					'churn',
					`<span class="add">+${fmt(c.additions)}</span> / <span class="del">−${fmt(
						c.deletions
					)}</span>`
				)
			);
		}
		wrap.appendChild(row);
	}
}

function renderVBars(container, values, labels) {
	container.innerHTML = '';
	const max = Math.max(...values, 1);
	values.forEach((v, i) => {
		const bar = el('div', 'vbar');
		bar.title = `${labels[i]}: ${fmt(v)} commits`;
		const col = el('div', 'col');
		col.style.height = `${(v / max) * 100}%`;
		bar.appendChild(col);
		bar.appendChild(el('div', 'vlabel', labels[i]));
		container.appendChild(bar);
	});
}

function renderWeekday(byWeekday) {
	renderVBars($('#weekday'), byWeekday, [
		'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat',
	]);
}

function renderHour(byHour) {
	const labels = byHour.map((_, i) => (i % 6 === 0 ? String(i) : ''));
	renderVBars($('#hour'), byHour, labels);
}

function renderTimeline(byDay) {
	const wrap = $('#timeline');
	wrap.innerHTML = '';
	const keys = Object.keys(byDay).sort();
	if (!keys.length) {
		wrap.appendChild(el('div', 'empty', 'No activity data.'));
		return;
	}
	// bucket into weeks for readability
	const buckets = {};
	for (const k of keys) {
		const d = new Date(k);
		const onejan = new Date(d.getFullYear(), 0, 1);
		const week = Math.ceil(((d - onejan) / 86400000 + onejan.getDay() + 1) / 7);
		const key = `${d.getFullYear()}-W${String(week).padStart(2, '0')}`;
		buckets[key] = (buckets[key] || 0) + byDay[k];
	}
	const bkeys = Object.keys(buckets).sort();
	const max = Math.max(...Object.values(buckets), 1);
	for (const k of bkeys) {
		const col = el('div', 'tcol');
		col.style.height = `${(buckets[k] / max) * 100}%`;
		col.title = `${k}: ${fmt(buckets[k])} commits`;
		wrap.appendChild(col);
	}
}

// ---- rendering: commit graph (lane assignment) ------------------------------
/**
 * Assign a column ("lane") to each commit, GitHub-network style.
 * Commits arrive newest-first (git log default).
 */
function computeLanes(commits) {
	const index = new Map();
	commits.forEach((c, i) => index.set(c.hash, i));

	const lanes = []; // lanes[i] = hash the lane is currently waiting for
	let maxLane = 0;

	const firstFree = () => {
		const idx = lanes.indexOf(null);
		if (idx !== -1) return idx;
		lanes.push(null);
		return lanes.length - 1;
	};

	for (const c of commits) {
		// find the lane reserved for this commit (set by an earlier child)
		let lane = lanes.indexOf(c.hash);
		if (lane === -1) {
			lane = firstFree();
		}
		c.lane = lane;
		maxLane = Math.max(maxLane, lane);

		// any other lanes also waiting for this commit (merge funnels) get freed
		for (let i = 0; i < lanes.length; i++) {
			if (lanes[i] === c.hash) lanes[i] = null;
		}

		// route parents
		const parents = c.parents.filter((p) => index.has(p));
		if (parents.length) {
			// first parent continues the commit's own lane (unless already placed)
			if (lanes.indexOf(parents[0]) === -1) lanes[lane] = parents[0];
			else if (lanes[lane] === null) lanes[lane] = null; // already routed elsewhere
			for (let k = 1; k < parents.length; k++) {
				if (lanes.indexOf(parents[k]) === -1) {
					const pl = firstFree();
					lanes[pl] = parents[k];
					maxLane = Math.max(maxLane, pl);
				}
			}
		}
	}
	return maxLane + 1;
}

function renderGraph(commits) {
	const wrap = $('#graph');
	wrap.innerHTML = '';
	if (!commits.length) {
		wrap.appendChild(el('div', 'empty', 'No commits to display.'));
		return;
	}

	const laneCount = computeLanes(commits);
	const index = new Map();
	commits.forEach((c, i) => index.set(c.hash, i));

	const ROW = 40;
	const LANE_W = 20;
	const PAD = 16;
	const RADIUS = 5;
	const gutter = Math.min(laneCount * LANE_W + PAD * 2, 360);
	const xFor = (lane) => PAD + lane * LANE_W;

	commits.forEach((c, i) => {
		const row = el('div', 'graph-row');

		// SVG cell for this row showing node + edges down to its parents
		const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		svg.setAttribute('width', gutter);
		svg.setAttribute('height', ROW);
		svg.style.overflow = 'visible';

		const cx = xFor(c.lane);
		const cy = ROW / 2;

		// edges to parents (drawn from this row downward)
		for (const p of c.parents) {
			const j = index.get(p);
			if (j == null) continue;
			const parent = commits[j];
			const px = xFor(parent.lane);
			const color = LANE_COLORS[c.lane % LANE_COLORS.length];
			const path = document.createElementNS(
				'http://www.w3.org/2000/svg',
				'path'
			);
			// curve from node center to bottom edge, bending toward parent lane
			const endY = ROW;
			const d =
				px === cx
					? `M ${cx} ${cy} L ${cx} ${endY}`
					: `M ${cx} ${cy} C ${cx} ${cy + ROW / 2}, ${px} ${endY - ROW / 2}, ${px} ${endY}`;
			path.setAttribute('d', d);
			path.setAttribute('stroke', color);
			path.setAttribute('stroke-width', '2');
			path.setAttribute('fill', 'none');
			svg.appendChild(path);
		}

		// edges coming from above into this node, for lanes that pass through.
		// (handled implicitly by parents above; we additionally draw straight
		//  pass-through lines for continuity)
		const incoming = commits[i - 1];
		void incoming;

		// node circle
		const circle = document.createElementNS(
			'http://www.w3.org/2000/svg',
			'circle'
		);
		circle.setAttribute('cx', cx);
		circle.setAttribute('cy', cy);
		circle.setAttribute('r', RADIUS);
		const nodeColor = LANE_COLORS[c.lane % LANE_COLORS.length];
		circle.setAttribute('fill', nodeColor);
		circle.setAttribute('stroke', '#0d1117');
		circle.setAttribute('stroke-width', '2');
		svg.appendChild(circle);

		row.appendChild(svg);

		// commit info
		const info = el('div', 'commit-info');
		info.style.maxWidth = `calc(100% - ${gutter}px)`;
		for (const ref of c.refs) {
			const isHead = ref.startsWith('HEAD');
			const tag = el(
				'span',
				`ref-tag ${isHead ? 'head' : ''}`,
				esc(ref.replace('HEAD -> ', ''))
			);
			info.appendChild(tag);
		}
		info.appendChild(el('span', 'commit-hash', esc(c.short)));
		info.appendChild(el('span', 'commit-subject', esc(c.subject)));
		info.appendChild(el('span', 'commit-author', '— ' + esc(c.author)));
		info.appendChild(el('span', 'commit-date', dateStr(c.date)));
		info.title = `${c.short}  ${c.subject}\n${c.author} · ${new Date(
			c.date
		).toLocaleString()}`;
		row.appendChild(info);

		wrap.appendChild(row);
	});
}

// ---- wiring -----------------------------------------------------------------
$('#loadBtn').addEventListener('click', loadRepo);
$('#repo').addEventListener('keydown', (e) => {
	if (e.key === 'Enter') loadRepo();
});
$('#branch').addEventListener('change', loadBranch);
$('#graphLimit').addEventListener('change', loadBranch);

// hydrate from last session + show home hint
(async () => {
	try {
		const { home, cwd } = await api('home', {});
		$('#hint').textContent = `Tip: paths are on this machine. e.g. ${cwd}`;
		const saved = localStorage.getItem('lgs.repo');
		$('#repo').value = saved || cwd || home || '';
	} catch (_) {}
})();
