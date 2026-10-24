// Build a draw.io viewer URL (#R = compressed XML, the format draw.io loads as is) for one page XML file.
// Usage: node viewer_url.mjs <drawio-mcp package dir> <page.xml>
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const [pkg, file] = process.argv.slice(2);
const require = createRequire(join(pkg, "package.json"));
const pako = require("pako");
const xml = readFileSync(file, "utf8");
const data = Buffer.from(pako.deflateRaw(encodeURIComponent(xml))).toString("base64");
process.stdout.write("https://viewer.diagrams.net/?lightbox=1&nav=1&border=20#R" + encodeURIComponent(data));
