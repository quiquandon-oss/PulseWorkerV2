import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKER_JS_PATH = join(__dirname, '..', '..', 'worker.js');

let _cachedSrc = null;
function getSource() {
  if (_cachedSrc === null) _cachedSrc = readFileSync(WORKER_JS_PATH, 'utf8');
  return _cachedSrc;
}

export function extractFunctions(...names) {
  const src = getSource();
  const pieces = [];
  for (const name of names) {
    const startMatch = src.match(new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`));
    if (!startMatch) throw new Error(`Could not find "function ${name}(" in worker.js`);
    const startIdx = startMatch.index;
    // Find the END of the parameter list first (matching close-paren,
    // tracking depth so a destructured object/array/default value inside
    // the params doesn't fool this) -- only THEN look for the function
    // body's opening brace. Scanning for the first "{" from startIdx
    // directly is wrong whenever a param is destructured, e.g.
    // `function f({ a, b }) { ... }`, since that "{" belongs to the
    // parameter, not the body.
    const parenStart = src.indexOf('(', startIdx);
    let parenDepth = 0, parenEnd = -1;
    for (let j = parenStart; j < src.length; j++) {
      if (src[j] === '(') parenDepth++;
      else if (src[j] === ')') { parenDepth--; if (parenDepth === 0) { parenEnd = j; break; } }
    }
    if (parenEnd === -1) throw new Error(`Could not find closing ")" for function ${name}`);
    const braceStart = src.indexOf('{', parenEnd);
    let depth = 0, i = braceStart;
    for (; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') { depth--; if (depth === 0) { i++; break; } }
    }
    pieces.push(src.slice(startIdx, i));
  }
  return pieces.join('\n\n');
}

// Extracts one or more top-level `const NAME = ...;` declarations by name —
// needed alongside extractFunctions when a function references module-level
// constants (e.g. decideSelection reading SELECTION_CRITICAL_Z). Matches
// through to the first top-level semicolon, handling nested {}/[] correctly
// so an object or array literal doesn't get truncated early.
export function extractConstants(...names) {
  const src = getSource();
  const pieces = [];
  for (const name of names) {
    const startMatch = src.match(new RegExp(`const\\s+${name}\\s*=`));
    if (!startMatch) throw new Error(`Could not find "const ${name} =" in worker.js`);
    const startIdx = startMatch.index;
    let depth = 0, i = startIdx, end = -1;
    for (; i < src.length; i++) {
      if (src[i] === '{' || src[i] === '[' || src[i] === '(') depth++;
      else if (src[i] === '}' || src[i] === ']' || src[i] === ')') depth--;
      else if (src[i] === ';' && depth === 0) { end = i + 1; break; }
    }
    if (end === -1) throw new Error(`Could not find terminating semicolon for const ${name}`);
    pieces.push(src.slice(startIdx, end));
  }
  return pieces.join('\n');
}

export function evalInScope(source, extraGlobals = {}) {
  const sandbox = { console, ...extraGlobals };
  const keys = Object.keys(sandbox);
  const fnNames = [...source.matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(m => m[1]);
  // Also expose top-level `const NAME = ...;` declarations (e.g. via
  // extractConstants) so tests can assert on constant values directly, not
  // just observe their effect through a function.
  const constNames = [...source.matchAll(/^const\s+([A-Za-z_$][\w$]*)\s*=/gm)].map(m => m[1]);
  const names = [...new Set([...fnNames, ...constNames])];
  const fn = new Function(...keys, `${source}\nreturn { ${names.join(',')} };`);
  return fn(...keys.map(k => sandbox[k]));
}
