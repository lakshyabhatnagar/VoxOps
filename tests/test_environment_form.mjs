import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor() { this.value = ''; this.children = []; this.listeners = {}; this.dataset = {}; this.disabled = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  async fire(type) { await this.listeners[type]({target: this, preventDefault() {}}); }
  reportValidity() { return this.checkValidity(); }
  setCustomValidity(value) { this.invalid = !!value; }
  checkValidity() {
    return !this.invalid && !(this.required && !this.value) &&
      (!this.value || !this.pattern || new RegExp(`^(?:${this.pattern})$`).test(this.value));
  }
}
const elements = new Map();
function element(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
const requests = [];
let schema = {requirements: [
  {field_id: 'secret', name: '<secret>', consumers: [{unit_id: 'api', phase: 'runtime'}], necessity: 'required', visibility: 'secret', resolution: 'missing', value_type: 'string', max_length: 100},
  {field_id: 'public', name: 'VITE_URL', consumers: [{unit_id: 'web', phase: 'build'}], necessity: 'required', visibility: 'public', resolution: 'default', value_type: 'url', max_length: 100},
], ambiguities: []};
const document = {getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], addEventListener() {}};
const context = vm.createContext({document, HTMLFormElement: class {}, FormData: class {}, URL,
  fetch: async (url, options = {}) => {
    requests.push({url, ...options});
    return {ok: true, status: 200, json: async () => ({schema, schema_digest: 'digest'})};
  }});
const page = readFileSync(new URL('../src/voxops/static/index.html', import.meta.url), 'utf8');
vm.runInContext(page.split('<script>')[1].split('</script>')[0], context);
element('env-run-id').value = 'run-1';
await element('env-load').fire('click');
assert.equal(requests[0].url, '/api/v1/artifact-runs/run-1/verification/env-form');
const fields = element('env-fields').children;
const [secret, publicInput] = [fields[2], fields[6]];
assert.equal(fields[1].textContent, '<secret>');
assert.equal(secret.type, 'password');
assert.equal(publicInput.type, 'url');
assert.match(fields[7].textContent, /Public frontend value/);
assert.equal(element('env-continue').disabled, true);
await element('env-form').fire('submit');
assert.equal(requests.length, 1);
secret.value = '   ';
await secret.fire('input');
assert.equal(element('env-continue').disabled, true);
secret.value = 'private-value';
await secret.fire('input');
assert.equal(element('env-continue').disabled, false);
publicInput.value = 'not a url';
await publicInput.fire('input');
assert.equal(element('env-continue').disabled, true);
publicInput.value = '';
await publicInput.fire('input');
await element('env-form').fire('submit');
assert.equal(requests.length, 2);
assert.equal(requests[1].url, '/api/v1/artifact-runs/run-1/verification/env-values');
assert.deepEqual(JSON.parse(requests[1].body), {schema_digest: 'digest', values: {secret: 'private-value'}});
assert.equal(secret.value, '');
assert.equal(element('env-form').hidden, true);
assert.equal(element('env-fields').children.length, 0);
await element('env-load').fire('click');
const cancelled = element('env-fields').children[2];
cancelled.value = 'another secret';
await element('env-cancel').fire('click');
assert.equal(cancelled.value, '');
schema = {...schema, ambiguities: [{kind: 'dynamic'}]};
await element('env-load').fire('click');
const blocked = element('env-fields').children[3];
blocked.value = 'value';
await blocked.fire('input');
assert.equal(element('env-continue').disabled, true);
await element('env-form').fire('submit');
assert.equal(requests.filter(request => request.method === 'POST').length, 1);
console.log('Environment form gating, masking, submission and clearing checks passed.');
