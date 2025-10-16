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
npm install --save-dev typescript ts-node ts-node-dev copyfiles @types/node @types/cors @types/express @types/lodash >/dev/null
npm install express lodash p-queue reflect-metadata dotenv uuid zod zod-to-json-schema class-transformer class-validator cors

echo "🛠️ Creating tsconfig.json..."
npx tsc --init >/dev/null

echo "📄 Creating src/main.ts..."
cat >src/main.ts << "EOF"
import 'reflect-metadata';

import cors from 'cors';
import express from 'express';

import { APP_PORT } from './generated/env';

const app = express();
app.use(cors());
app.use(express.json());

app.listen(APP_PORT, async () => {
	console.log(`Server running on port ${APP_PORT}`);
});
EOF

echo "📄 Creating .env..."
cat >.env << "EOF"
APP_PORT="3000"
EOF

echo "🛠️ Creating tsconfig.json..."
cat >tsconfig.json << "EOF"
{
  "compilerOptions": {
    "rootDir": "src",
    "outDir": "dist",
    "module": "commonjs",
    "target": "esnext",
    "moduleResolution": "node",
    "types": [],
    "sourceMap": true,
    "declaration": true,
    "declarationMap": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "experimentalDecorators": true,
    "emitDecoratorMetadata": true,
    "strict": true,
    "jsx": "react-jsx",
    "verbatimModuleSyntax": false,
    "isolatedModules": true,
    "noUncheckedSideEffectImports": true,
    "moduleDetection": "force",
    "skipLibCheck": true,
    "esModuleInterop": true,
    "resolveJsonModule": true,
  },
  "include": [
    "src/**/*",
  ],
  "exclude": [
    "scripts/**/*",
    "node_modules",
    "dist"
  ]
}
EOF

echo "📄 Creating scripts/generate-env.ts..."
mkdir scripts
cat >scripts/generate-env.ts << "EOF"
import fs from 'fs';
import path from 'path';
import dotenv from 'dotenv';

// 1. Load .env file
const envPath = path.resolve(process.cwd(), '.env');
const result = dotenv.config({ path: envPath });

if (result.error) {
	throw result.error;
}

const parsed = result.parsed || {};

// 2. Generate TypeScript file
const outDir = path.resolve(process.cwd(), 'src/generated');
fs.mkdirSync(outDir, { recursive: true });

const outFile = path.join(outDir, 'env.ts');

// For each key in .env, emit a const
const fileContent = `// ⚠️ This file is auto-generated from .env. Do not edit manually.

${Object.entries(parsed)
	.map(
		([key, value]) => `export const ${key} = ${JSON.stringify(value)} as const;`
	)
	.join('\n')}
`;

fs.writeFileSync(outFile, fileContent);

console.log('✅ Environment file generated:', outFile);
EOF

echo "🐒 Adding scripts..."
npm pkg set scripts.copy-assets="copyfiles -u 1 assets/**/* dist"
npm pkg set scripts.generate-env="ts-node scripts/generate-env.ts"
npm pkg set scripts.prebuild="npm run generate-env"
npm pkg set scripts.prestart="npm run generate-env"
npm pkg set scripts.start="ts-node-dev --respawn --transpile-only src/main.ts"
npm pkg set scripts.build="tsc && npm run copy-assets"

echo "🧑‍💻 Initializing git"
git init >/dev/null

echo "📃 Writing .gitignore..."
cat >.gitignore <<'EOF'
node_modules/
dist/
.env
.zshrc.d
EOF

echo -e "\n✅ Done! To run your project:\n cd $PROJECT_NAME && npm run start"
