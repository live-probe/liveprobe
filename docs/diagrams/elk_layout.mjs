// Apply the draw.io MCP server's own layout passes to raw page XML files.
// Usage: node elk_layout.mjs <drawio-mcp package dir> <in.xml> <out.xml> <vertical|horizontal|route>
//   vertical / horizontal : ELK layered layout (places nodes and routes edges)
//   route                 : keep hand-placed nodes, re-route edges around shapes (libavoid)
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const [pkg, input, output, mode = "vertical"] = process.argv.slice(2);
const xml = readFileSync(input, "utf8");
let out;
if (mode === "route") {
  const { routeXml } = await import(pathToFileURL(join(pkg, "src", "libavoid-pass.js")).href);
  out = await routeXml(xml);
} else {
  const { layoutXml } = await import(pathToFileURL(join(pkg, "src", "elk-pass.js")).href);
  out = await layoutXml(xml, { direction: mode });
}
writeFileSync(output, out);
console.log(`${mode}: ${input} -> ${output}; changed=${out !== xml}`);
