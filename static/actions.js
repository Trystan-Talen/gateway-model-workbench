/* Explicit delegation: attributes carry data only; never eval handler strings. */
'use strict';
const UI_ACTIONS = Object.freeze({
  openRoutingCreate: ()=>openRoutingCreate(),
  openGatewayCreate: node=>openGatewayCreate(node.dataset.arg0||'single'),
  openQualityCreate: node=>openQualityCreate(node.dataset.arg0||'single'),
  addComparisonGroup: (node)=>addComparisonGroup(),
  cleanupStorage: (node)=>cleanupStorage(),
  closeAccountModal: (node)=>closeAccountModal(),
  closeDiagnostics: (node)=>closeDiagnostics(),
  closeModal: (node)=>closeModal(node.dataset.arg0),
  closeQualityEvidence: (node)=>closeQualityEvidence(),
  closeQualitySuite: (node)=>closeQualitySuite(),
  closeRaw: (node)=>closeRaw(),
  closeStorageModal: (node)=>closeStorageModal(),
  deleteAccount: (node)=>deleteAccount(node.dataset.arg0),
  deleteComparison: (node)=>deleteComparison(node.dataset.arg0),
  deleteQuality: (node)=>deleteQuality(node.dataset.arg0),
  deleteRouting: (node)=>deleteRouting(node.dataset.arg0),
  deleteTask: (node)=>deleteTask(node.dataset.arg0),
  download: (node)=>{location.href=node.dataset.arg0},
  loadDiagnostics: (node)=>loadDiagnostics(),
  loadRoutingCorpus: (node)=>loadRoutingCorpus(),
  loadStorage: (node)=>loadStorage(),
  logout: (node)=>logout(),
  openAccountModal: (node)=>openAccountModal(),
  openCreateForContext: (node)=>openCreateForContext(),
  openDiagnostics: (node)=>openDiagnostics(),
  openStorageModal: (node)=>openStorageModal(),
  refreshQualityModels: (node)=>refreshQualityModels(Number(node.dataset.arg0)),
  removeGroup: (node)=>{node.closest('.group-row').remove();normalizeComparisonRoles()},
  retryTask: (node)=>retryTask(node.dataset.arg0),
  selectComparison: (node)=>selectComparison(node.dataset.arg0),
  selectQuality: (node)=>selectQuality(node.dataset.arg0),
  selectRouting: (node)=>selectRouting(node.dataset.arg0),
  selectTask: (node)=>selectTask(node.dataset.arg0),
  setComparisonTab: (node)=>setComparisonTab(node.dataset.arg0,node.dataset.arg1),
  setQualityTab: (node)=>setQualityTab(node.dataset.arg0,node.dataset.arg1),
  setRecordFilter: (node)=>setRecordFilter(node.dataset.arg0),
  setRoutingTab: (node)=>setRoutingTab(node.dataset.arg0,node.dataset.arg1),
  showQualityCase: (node)=>showQualityCase(Number(node.dataset.arg0),Number(node.dataset.arg1)),
  showQualityCaseById: (node)=>showQualityCaseById(Number(node.dataset.resource),node.dataset.case),
  showQualityHistoryReference: (node)=>showQualityHistoryReference(node.dataset.arg0),
  showQualityMethodology: (node)=>showQualityMethodology(),
  showQualityMethodology1: (node)=>showQualityMethodology(node.dataset.arg0),
  showQualitySuite: (node)=>showQualitySuite(),
  showRaw: (node)=>showRaw(node.dataset.arg0,Number(node.dataset.arg1)),
  stopQuality: (node)=>stopQuality(node.dataset.arg0),
  stopRouting: (node)=>stopRouting(node.dataset.arg0),
  stopTask: (node)=>stopTask(node.dataset.arg0),
  toggleConfig: (node)=>toggleConfig(false),
  togglePasswordVisibility: (node)=>togglePasswordVisibility(node.dataset.arg0,node)
});
for (const eventName of ['click', 'change']) {
  document.addEventListener(eventName, event => {
    const node = event.target.closest?.(`[data-${eventName}-action]`);
    if (!node || node.disabled) return;
    const action = UI_ACTIONS[node.getAttribute(`data-${eventName}-action`)];
    if (!Object.hasOwn(UI_ACTIONS, node.getAttribute(`data-${eventName}-action`))) return;
    event.preventDefault();
    Promise.resolve().then(() => action(node)).catch(error => toast(error.message || '操作失败'));
  });
}
