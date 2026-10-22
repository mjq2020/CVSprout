// Exercise actual batch-review requests and the automatic review continuation default.
import assert from "node:assert/strict";
import { bindReviewPage, reviewContinuationMarkup, shouldContinueReview } from "../static/review-ui.js";

const listeners = new Map();
const content = { isConnected: true, innerHTML: "", addEventListener() {} };
const submit = {}, cancel = {}, reasonField = { value: "" }, confirmation = { checked: false };
const nodes = new Map([
  ["#batch-review-content", content], ["#modal-submit", submit], ["#modal-cancel", cancel],
  ["#batch-review-reason", reasonField], ["#batch-review-confirm", confirmation],
]);
globalThis.document = { addEventListener: (type, handler) => listeners.set(type, handler), querySelector: selector => nodes.get(selector) || null };
globalThis.localStorage = { getItem: () => "false" };
assert.equal(shouldContinueReview(), true, "A previous opt-out no longer disables automatic review continuation");
assert.equal(reviewContinuationMarkup(false), "");
const hint = reviewContinuationMarkup(true);
assert(hint.includes("Y 通过") && hint.includes("X 退回") && hint.includes("自动下一张"));
assert(!hint.includes("checkbox"), "Automatic continuation is not presented as an opt-in");

for (const [action, reason] of [["reject", ""], ["reject", "   "], ["reject", "  请补充漏标  "], ["approve", ""]]) {
  const requests = [];
  const state = { currentSection: "data", project: { id: 3 }, selectionEpoch: 7, session: {},
    selectedAssetIds: new Set([11]), assets: [{ id: 11, filename: "sample.jpg", revision: 2, review_status: "submitted" }] };
  let modal, renders = 0;
  reasonField.value = reason; confirmation.checked = false;
  const item = { asset_id: 11, filename: "sample.jpg", review_status: "submitted", status: "eligible", message: "可处理" };
  bindReviewPage({
    state, escapeHtml: value => String(value ?? ""), jsonOptions: (method, body) => ({ method, body }),
    notify: message => assert.fail(message), closeModal() {}, renderDataPage: () => { renders++; },
    openModal: options => { modal = options; options.onReady(); },
    api: async (path, options) => {
      requests.push({ path, ...options });
      if (path.endsWith("/preview")) return { counts: { eligible: 1 }, items: [item] };
      assert(path.endsWith("/batch"));
      return { counts: { succeeded: 1 }, items: [{ ...item, status: "succeeded", asset: { id: 11, revision: 3, review_status: action === "reject" ? "rejected" : "approved" } }] };
    },
  });
  listeners.get("click")({ target: { closest: selector => selector === "[data-batch-review]" ? { dataset: { batchReview: action }, disabled: false } : null } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(submit.disabled, false, "The actual preview finishes before confirmation");
  if (action === "reject") {
    assert(content.innerHTML.includes("修改要求（可选"));
    assert(!content.innerHTML.includes("必填"));
  } else {
    await assert.rejects(modal.onSubmit(), /请先确认/);
    assert.equal(requests.length, 1, "Approving a batch still requires explicit quality confirmation");
    confirmation.checked = true;
  }
  await modal.onSubmit();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.action, action);
  assert.equal(requests[1].body.reason, reason.trim(), "Empty reasons are sent successfully; supplied reasons are trimmed");
  assert.deepEqual(requests[1].body.assets, [{ asset_id: 11, expected_revision: 2 }]);
  assert.equal(state.assets[0].review_status, action === "reject" ? "rejected" : "approved");
  assert.equal(state.selectedAssetIds.size, 0);
  assert.equal(renders, 1);
}

console.log("PASS automatic review continuation regardless old preference; optional batch rejection reasons; trimmed notes; successful state synchronization; explicit batch approval confirmation");
