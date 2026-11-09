import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const elements = new Map();
const buttons = [{disabled: false}];
const requests = [];
let nextResponse = {};

// Pretends the test form contains valid input.
// Returns true so the test can reach the code that sends the request.
function valid() { return true; }

// Does nothing when the test page tries to focus a field or stop an event.
// These browser actions are not needed to check which API request is sent.
function noop() {}

// Creates a small stand-in for a page field and remembers it by its ID.
// Lets the test inspect text and values written by the real page code.
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, reportValidity: valid,
    focus: noop, replaceChildren: noop, append: noop, addEventListener: noop,
  });
  return elements.get(id);
}

// Returns the test buttons when the page asks for its action buttons.
// Lets the test check that buttons become available again after a request.
function queryButtons() { return buttons; }

// Holds the action and input values for a pretend form submission.
// Gives the real page handler the same fields it would read from a browser form.
class Form {
  // Sets up one test form with its chosen action and input values.
  // Stores the values by field name so the page handler can read them.
  constructor(action, fields) { this.dataset = {action}; this.fields = new Map(fields); }
}

// Stands in for the browser helper that reads submitted form fields.
// Returns the test form values with the lookup methods the real handler uses.
class Fields {
  // Provides the values already saved in the pretend form.
  // Lets the real page handler read those values without opening a browser.
  constructor(form) { return form.fields; }
}

// Returns the API response chosen by the current test.
// Avoids making a real network request or calling an AI service.
async function json() { return nextResponse; }

// Records the URL and body that the page tries to send to the server.
// Returns a pretend successful response so the page can finish handling the request.
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
buttons.push(element('env-continue'));
context.updateEnvironmentValidity();
assert.equal(element('env-continue').disabled, true);

// Submits a pretend form through the real page handler.
// Checks that the page restores its buttons after the request finishes.
async function submit(action, fields = [], operation = '') {
  await context.submit({target: new Form(action, fields), submitter: {value: operation},
    preventDefault: noop});
  assert.equal(buttons[0].disabled, false);
  assert.equal(element('env-continue').disabled, true);
  return requests.at(-1);
}

nextResponse = {detected_units: [], unresolved_ambiguities: [], deterministic_only: true};
let sent = await submit('analyze', [['transcript', 'analyze this repository'], ['use_llm', 'on']]);
assert.equal(sent.url, '/api/v1/repositories/analyze');
assert.deepEqual(JSON.parse(sent.body), {path: '/workspace/example', use_llm: true,
  transcript: 'analyze this repository'});
assert.match(element('summary').textContent, /0 deployable unit/);
nextResponse = {detected_units: [{root_path: '.', framework: 'fastapi', role: 'http_service',
  runtime: {start_command: 'uv run uvicorn app.main:app --workers 1', command_source: 'explicit',
    prerequisite_commands: ['uv run alembic upgrade head']}}],
  unresolved_ambiguities: [], deterministic_only: true};
await submit('analyze', [['transcript', '']]);
assert.match(element('summary').textContent, /Before starting \(not executed\):\nuv run alembic upgrade head/);
assert.match(element('summary').textContent, /Start \(explicit\): uv run uvicorn app.main:app --workers 1/);

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
