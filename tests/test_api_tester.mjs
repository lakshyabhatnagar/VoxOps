import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const elements = new Map();
const buttons = [{disabled: false}];
const requests = [];
let nextResponse = {};

// Why: Supply the browser's basic input-validation result for request-contract tests.
// How: Accept valid fixture inputs without requiring a browser renderer.
function valid() { return true; }

// Why: Ignore browser event and focus side effects unrelated to request payloads.
// How: Provide a no-op method for the minimal DOM fixture.
function noop() {}

// Why: Exercise response rendering and auto-filled fields without a DOM dependency.
// How: Keep small mutable elements keyed by their actual page IDs.
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, reportValidity: valid,
    focus: noop, replaceChildren: noop, append: noop,
  });
  return elements.get(id);
}

// Why: Verify pending requests restore the page's action buttons.
// How: Return the same fixture button for each document query.
function queryButtons() { return buttons; }

// Why: Represent browser form inputs in their submitted shape.
// How: Store the action and name/value pairs used by the real page handler.
class Form {
  // Why: Prepare an individual form submission without a browser runtime.
  // How: Record its dataset and fields for the FormData fixture.
  constructor(action, fields) { this.dataset = {action}; this.fields = new Map(fields); }
}

// Why: Preserve native FormData get/has behavior in the isolated test context.
// How: Return the form's Map, whose lookup interface matches the handler's usage.
class Fields {
  // Why: Expose submitted form entries to the unchanged page code.
  // How: Return the existing name/value Map from the fixture form.
  constructor(form) { return form.fields; }
}

// Why: Return typed API fixtures without making network or paid-provider calls.
// How: Resolve the response data selected by the current request test.
async function json() { return nextResponse; }

// Why: Verify exactly which API operations the page would send.
// How: Capture fetch arguments and return an ordinary successful JSON response.
async function request(url, options) {
  requests.push({url, ...options});
  return {ok: true, status: 200, json};
}

const page = readFileSync(new URL('../src/voxops/static/index.html', import.meta.url), 'utf8');
const context = vm.createContext({
  HTMLFormElement: Form, FormData: Fields, fetch: request,
  document: {getElementById: element, querySelectorAll: queryButtons,
    addEventListener: noop, createElement: element},
});
vm.runInContext(page.split('<script>')[1].split('</script>')[0], context);
element('path').value = '/workspace/example';

// Why: Exercise the real submit handler across endpoint and cleanup-policy branches.
// How: Dispatch a fixture submit event and assert all controls recover after the request.
async function submit(action, fields = [], operation = '') {
  await context.submit({target: new Form(action, fields), submitter: {value: operation},
    preventDefault: noop});
  assert.equal(buttons[0].disabled, false);
  return requests.at(-1);
}

nextResponse = {detected_units: [], unresolved_ambiguities: [], deterministic_only: true};
let sent = await submit('analyze', [['transcript', 'analyze this repository'], ['use_llm', 'on']]);
assert.equal(sent.url, '/api/v1/repositories/analyze');
assert.deepEqual(JSON.parse(sent.body), {path: '/workspace/example', use_llm: true,
  transcript: 'analyze this repository'});
assert.match(element('summary').textContent, /0 deployable unit/);

nextResponse = {id: 'a'.repeat(32), branch: 'voxops/test', status: 'active', location: '/tmp/test'};
await submit('create-worktree');
assert.equal(element('worktree-id').value, nextResponse.id);
sent = await submit('worktree', [['identifier', nextResponse.id]], 'remove');
assert.equal(sent.method, 'DELETE');
assert.match(sent.url, /discard_changes=false$/);
sent = await submit('worktree', [['identifier', nextResponse.id], ['discard_changes', 'on']], 'remove');
assert.match(sent.url, /discard_changes=true$/);
const before = requests.length;
await submit('worktree', [['identifier', '../escape']], 'inspect');
assert.equal(requests.length, before);

nextResponse = {provider: 'transcript_stub', text: '<script>untrusted</script>'};
await submit('transcribe', [['mode', 'transcript'], ['text', nextResponse.text], ['language', '']]);
assert.match(element('summary').textContent, /<script>untrusted<\/script>/);
assert.equal(JSON.parse(requests.at(-1).body).mode, 'transcript');
await submit('service', [], 'ready');
assert.equal(requests.at(-1).url, '/ready');
console.log('API tester request, rendering, ID and cleanup-policy checks passed.');
