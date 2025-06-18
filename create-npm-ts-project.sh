#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${1:-my-ts-app}"

if [ -d "$PROJECT_NAME" ]; then
  echo "❌ Directory \"$PROJECT_NAME\" already exists." >&2
  exit 1
fi

echo "📁 Creating project: $PROJECT_NAME"
mkdir -p "$PROJECT_NAME/src"
cd "$PROJECT_NAME"

echo "📦 Initializing npm..."
npm init -y >/dev/null

echo "🔧 Installing TypeScript and tools..."
npm install --save-dev typescript ts-node @types/node >/dev/null

echo "🛠️ Creating tsconfig.json..."
npx tsc --init \
  --rootDir src --outDir dist \
  --esModuleInterop --resolveJsonModule --module commonjs --target ES2020 >/dev/null

echo "📄 Creating src/main.ts..."
cat >src/main.ts <<'EOF'
const greet = (name: string): string => `Hello, ${name}!`;

console.log(greet("world"));
EOF

echo "🧑‍💻 Initializing git"
git init >/dev/null

echo "📃 Writing .gitignore..."
cat >.gitignore <<'EOF'
node_modules/
dist/
EOF

echo "📝 Adding start script to package.json..."
node -e '
const fs = require("fs");
const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
pkg.scripts = pkg.scripts || {};
pkg.scripts.start = "ts-node src/main.ts";
fs.writeFileSync("package.json", JSON.stringify(pkg, null, 2));
'

echo -e "\n✅ Done! To run your project:\n   cd $PROJECT_NAME && npm run start"
