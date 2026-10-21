#!/usr/bin/env node
// Runs the official n8n community-package static analyzer
// (`@n8n/scan-community-package`) against the LOCAL packed tarball, instead
// of `npx @n8n/scan-community-package <name>@<version>` which only works
// for a package that is already published to npm (it fetches the tarball
// and an npm-provenance attestation from the registry). Before this
// package's first publish there is nothing on the registry to scan, so CI
// reuses the scanner's own exported `analyzePackage()` — scoped to the same
// `**/*.js` + `package.json` file set the CLI's own tarball leg lints
// instead; several node rules false-positive on filenames when pointed at
// `.d.ts` output) — against a local `npm pack` tarball extracted to a temp
// directory.
//
// The scanner pulls in its own pinned `typescript` package alias, which
// conflicts with this project's own `tsc` bin when installed as a project
// devDependency (breaks `npm run build`) — install it into an isolated
// directory instead (see ci.yml / publish.yml) and pass its scanner.mjs
// path as the second argument here.
//
// Usage: node scripts/scan-community-package.mjs <tarball-path> [scanner.mjs-path]
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const tarballPath = process.argv[2];
const scannerModulePath = process.argv[3];
if (!tarballPath) {
	console.error(
		'Usage: node scripts/scan-community-package.mjs <tarball-path> [scanner.mjs-path]',
	);
	process.exit(1);
}
if (!fs.existsSync(tarballPath)) {
	console.error(`Tarball not found: ${tarballPath}`);
	process.exit(1);
}

const scannerSpecifier = scannerModulePath
	? pathToFileURL(path.resolve(scannerModulePath)).href
	: '@n8n/scan-community-package/scanner/scanner.mjs';

const { analyzePackage } = await import(scannerSpecifier);

const extractDir = fs.mkdtempSync(path.join(os.tmpdir(), 'scan-community-package-'));

try {
	const tar = spawnSync('tar', ['-xzf', path.resolve(tarballPath), '-C', extractDir, '--strip-components=1'], {
		stdio: 'inherit',
	});
	if (tar.status !== 0) {
		console.error('Failed to extract tarball for scanning.');
		process.exit(1);
	}

	const result = await analyzePackage(extractDir, ['**/*.js', 'package.json']);

	if (result.passed) {
		console.log(`✅ ${path.basename(tarballPath)} passed community-package static analysis`);
		process.exit(0);
	}

	console.log(`❌ ${path.basename(tarballPath)} failed community-package static analysis`);
	console.log(`Reason: ${result.message}`);
	if (result.details) {
		console.log('\nDetails:');
		console.log(result.details);
	}
	process.exit(1);
} finally {
	fs.rmSync(extractDir, { recursive: true, force: true });
}
