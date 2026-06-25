# local-git-stats 📊

A tiny, **zero-dependency** Node app that gives you a nice dashboard of
statistics for any git repository on your machine — pick a repo, pick a branch,
and explore.

It shells out to your local `git` binary under the hood, so there's nothing to
install and nothing leaves your machine.

## Features

- **Pick any local repo** by path (supports `~` expansion).
- **Branch selector** — local and remote branches, sorted by most recent.
- **Summary cards** — total commits, contributors, active days, project span,
  lines added/removed.
- **Commits per contributor** — ranked bar chart with avatars and line churn.
- **Activity patterns** — commits by weekday and by hour of day.
- **Commit activity over time** — weekly timeline.
- **Commit graph / tree** — a GitHub-network-style lane graph rendered with SVG,
  including branch/tag/HEAD decorations.

## Requirements

- [Node.js](https://nodejs.org/) ≥ 16
- `git` available on your `PATH`

## Usage

```bash
cd local-git-stats

# start the server (default port 4321)
node server.js

# or pick a port and auto-open your browser
node server.js --port 8080 --open
```

Then open <http://localhost:4321>, paste the path to a local repository, hit
**Load repo**, and choose a branch.

### CLI flags

| Flag             | Description                          | Default |
| ---------------- | ------------------------------------ | ------- |
| `--port`, `-p`   | Port to listen on                    | `4321`  |
| `--open`, `-o`   | Open the dashboard in your browser   | off     |
| `--help`, `-h`   | Show usage                           |         |

## How it works

`server.js` is a small Node `http` server (no frameworks) that exposes a few
JSON endpoints. Each one runs read-only `git` commands (`git -C <repo> …`) via
`execFile` — no shell, so repo paths and branch names can't inject anything:

| Endpoint         | Git used                                            |
| ---------------- | --------------------------------------------------- |
| `/api/validate`  | `rev-parse --show-toplevel`, `for-each-ref`         |
| `/api/overview`  | `shortlog -sne`, `log --numstat`, `rev-list --count`|
| `/api/graph`     | `log --pretty=… <branch>`                            |

The frontend (`public/`) is plain HTML/CSS/JS — the commit graph lane layout and
SVG rendering live in `public/app.js`.

> **Note:** line-churn stats scan up to the most recent 4,000 commits and the
> graph is capped (100–1000 commits, selectable) to stay fast on large repos.
> The total commit count is always exact.

## Privacy

Everything runs locally against your own repositories. There are no external
network calls and no telemetry.
