// Research Lab simplification: label fidelity, navigation structure and safety properties of the two pages.
import { describe, it, expect } from 'vitest';
import vm from 'node:vm';
import { LEARNING_LAB_HTML } from '../learning/learning-ui.js';
import { VALIDATION_TEXT, CANDIDATE_STATUSES } from '../learning/learning-method.js';
import worker from '../worker.js';

const script = LEARNING_LAB_HTML.split('<script>')[1].split('</script>')[0];
const literal = (name) => {   // evaluate one `var NAME = {...};` / `[...]` literal from the page script
  const m = script.match(new RegExp(`var ${name} = (\\{[^;]*\\}|\\[[^;]*\\]);`));
  if (!m) throw new Error(`${name} not found`);
  return vm.runInNewContext(`(${m[1]})`);
};
async function advancedHtml() {
  const res = await worker.fetch(new Request('https://w.test/research-lab/advanced'), {}, { waitUntil() {} });
  return res.text();
}

describe('primary Research Lab: wording mirrors backend states', () => {
  it('validation labels are exactly the backend VALIDATION_TEXT', () => {
    expect(literal('VTEXT')).toEqual({ ...VALIDATION_TEXT });
  });
  it('every backend candidate status has a plain label, and no extra status is invented', () => {
    expect(Object.keys(literal('CSTATUS_TEXT')).sort()).toEqual([...CANDIDATE_STATUSES].sort());
    expect(literal('CSTATUS_TEXT').DATA_COLLECTION_REQUIRED).toBe('Data collection required');
  });
  it('the final-validation list holds only verdict statuses (interim accuracy is not shown before them)', () => {
    expect(literal('FINAL_VALIDATION')).toEqual(['SUPPORTED', 'NOT_SUPPORTED', 'INCONCLUSIVE']);
    expect(script).toContain("v.current_v1 && FINAL_VALIDATION.indexOf(v.status) >= 0");
  });
  it('the page contains no control characters (template-literal escapes are intact)', () => {
    expect(/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(LEARNING_LAB_HTML)).toBe(false);
    expect(script).toContain('/\\b[A-Z][A-Z_]{3,}\\b/g');
  });
});

describe('primary Research Lab: structure and actions', () => {
  it('keeps the three sections and a single link to Advanced', () => {
    expect(literal('TABS')).toEqual(['Market', 'Research', 'Learning']);
    expect(script.match(/href="\/research-lab\/advanced"/g)).toHaveLength(1);
  });
  it('approve is disabled until the backend-required acknowledgement is ticked', () => {
    expect(script).toContain("'<button class=\"btn\" data-d=\"APPROVE\"' + (needAck ? ' disabled' : '') + '>Approve V1 change</button>'");
    expect(script).toContain('ackEl.onchange = function () { apEl.disabled = !ackEl.checked; };');
    expect(script).toMatch(/if \(c\.status === 'PENDING_REVIEW' && proto\)/);   // decisions only for PENDING_REVIEW
    expect(script).toContain("(proxy ? '' : '<button class=\"btn\" data-d=\"APPROVE\"");    // never for a proxy
  });
  it('a signal prototype shows the data requirement and no numeric impact', () => {
    expect(script).toContain('id="newSignal"');
    expect(script).toContain("proto ? 'Not calculable yet: historical data required'");
    expect(script).toContain('No Proposed V1 is shown and no V1 numerical adjustment was applied.');
  });
  it('verified evidence, AI hypotheses and human-confirmed findings are labelled distinctly', () => {
    for (const s of ['src ver', 'src ai', 'src hum', 'AI hypothesis &middot; not confirmed', 'Confirmed by you', 'Collected news']) expect(script).toContain(s);
    expect(script).toContain('CryptoPulse never sends anything to an AI by itself.');
  });
  it('writes still ride only on the session cookie and the custom header', () => {
    expect(LEARNING_LAB_HTML).not.toMatch(/tokenInput|type="password"|Authorization|Bearer/);
    expect(script).toContain("'X-CryptoPulse-Research': '1'");
    for (const p of ['/api/learning/findings', '/api/learning/candidates', '/api/learning/candidate/update', '/api/learning/candidate/decide']) expect(script).toContain(p);
  });
});

describe('primary Research Lab: read-only visitors (staging review finding)', () => {
  it('signed-out visitors get a read-only hint next to every action that saves, and signed-in users never do', () => {
    expect(script).toContain("function roHint() { return signedIn ? '' :");
    expect(script.match(/<h2>Your decision<\/h2>' \+ roHint\(\)/g)).toHaveLength(2);   // both decision cards
    expect(script).toContain("roHint() + '<div class=\"btns\"><button class=\"btn\" id=\"confirm\">");
    expect(script).toContain("(editable ? roHint() : '')");
    expect(script.match(/function roHint[^\n]*/)[0]).not.toMatch(/not signed in|admin token/i);   // walkthrough prompt detector
    expect(script).toContain('renderSession(); if (market && !draft) render();');
  });
  it('the learning list uses the three decision groups', () => {
    for (const g of ["'Needs your decision'", "'In progress'", "'Decided'"]) expect(script).toContain(g);
  });
});

describe('Advanced page: one grouped home, every technical page kept', () => {
  it('groups all 12 pages, each exactly once, in three sections', async () => {
    const html = await advancedHtml();
    const pages = JSON.parse(html.match(/var PAGES = (\[[^\]]*\]);/)[1].replace(/'/g, '"'));
    expect(pages).toHaveLength(12);
    const groups = html.slice(html.indexOf('var GROUPS = ['), html.indexOf('function groupOf('));
    for (const p of pages) expect(groups.split(`['${p}', '`).length - 1, p).toBe(1);
    for (const g of ['Data &amp; Sources', 'Experiments &amp; Validation', 'System Diagnostics']) expect(html).toContain(`title: '${g}'`);
    expect(html).toContain("var current = 'Home';");
    expect(html).toContain('href="/research-lab"');            // a way back to the primary page
  });
  it('every page render function is still dispatched', async () => {
    const html = await advancedHtml();
    for (const f of ['renderDashboard', 'renderExperiment5', 'renderSentiment', 'renderMarket', 'renderResults', 'renderTimeline', 'renderMethodology', 'renderEvents', 'renderEvidence', 'renderSources', 'renderPipeline', 'renderStage7']) {
      expect(html).toMatch(new RegExp(`return ${f}\\(\\);`));
    }
    expect(/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(html)).toBe(false);
  });
});
