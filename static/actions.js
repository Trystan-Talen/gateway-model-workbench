/* Explicit delegation: attributes carry data only; never eval handler strings. */
'use strict';
const UI_ACTIONS = Object.freeze({
  toggleRecords: node=>{
    const panel=document.getElementById('recordPanel');
    const collapsed=panel.classList.toggle('records-collapsed');
    node.setAttribute('aria-expanded',String(!collapsed));
    node.setAttribute('aria-label',collapsed?'展开记录列表':'收起记录列表');
    node.textContent=collapsed?'展开':'收起';
  },
  selectTpm: node=>window.TpmUI?.select(node.dataset.arg0),
  setTpmFilter: node=>window.TpmUI?.setFilter(node.dataset.arg0),
  setTpmTab: node=>window.TpmUI?.setTab(node.dataset.arg1),
  closeTpmCreate: ()=>window.TpmUI?.closeCreate(),
  previewTpm: ()=>window.TpmUI?.preview(),
  loadTpmModels: ()=>window.TpmUI?.loadModels(),
  setTpmModelMode: node=>window.TpmUI?.setModelMode(node.dataset.arg0),
  calibrateTpm: ()=>window.TpmUI?.calibrate(),
  adjustTpmPlan: node=>window.TpmUI?.adjustPlan(node.dataset.arg0),
  startTpm: node=>window.TpmUI?.start(node.dataset.arg0),
  stopTpm: node=>window.TpmUI?.stop(node.dataset.arg0),
  repeatTpm: node=>window.TpmUI?.repeat(node.dataset.arg0),
  openRoutingCreate: ()=>openRoutingCreate(),
  selectConnection: node=>window.ConnectionUI?.select(node.dataset.arg0),
  openConnectionEdit: node=>window.ConnectionUI?.openEdit(node.dataset.arg0),
  openConnectionCreate: ()=>window.ConnectionUI?.openCreate(),
  deleteConnection: node=>window.ConnectionUI?.remove(node.dataset.arg0),
  closeConnectionCreate: ()=>window.ConnectionUI?.closeCreate(),
  saveConnection: ()=>window.ConnectionUI?.save(),
  revealConnectionKey: node=>window.ConnectionUI?.revealKey(node),
  refreshConnections: ()=>window.ConnectionUI?.refresh(),
  selectSavedConnection: node=>selectSavedConnection(node.dataset.arg0,node.dataset.arg1,node),
  saveCurrentConnection: node=>saveCurrentConnection(node.dataset.arg0,node.dataset.arg1,node),
  manageSavedConnections: node=>manageSavedConnections(node.dataset.arg0,node.dataset.arg1,node),
  selectFingerprintConnection: node=>window.FingerprintUI?.selectConnection(node),
  saveFingerprintConnection: ()=>window.FingerprintUI?.saveConnection(),
  openGatewayCreate: node=>openGatewayCreate(node.dataset.arg0||'single'),
  openQualityCreate: node=>openQualityCreate(node.dataset.arg0||'single'),
  addComparisonGroup: (node)=>addComparisonGroup(),
  applyTemplate: node=>applyTemplate(node.dataset.arg0),
  toggleTemplatePicker: ()=>toggleTemplatePicker(),
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
  loadQualitySuiteSummary: ()=>loadQualitySuiteSummary(isCompareCreate()),
  setQualityModelMode: node=>setQualityModelMode(Number(node.dataset.arg0)),
  revealRoutingKey: (node)=>revealRoutingKey(node.dataset.arg0,node),
  changeComparisonRole: node=>changeComparisonRole(node),
  removeGroup: node=>removeComparisonGroup(node),
  retryTask: (node)=>retryTask(node.dataset.arg0),
  repeatTest: node=>window.RepeatUI?.start(node.dataset.arg0,node.dataset.arg1),
  submitRepeat: ()=>window.RepeatUI?.submit(),
  closeRepeat: ()=>window.RepeatUI?.close(),
  selectComparison: (node)=>selectComparison(node.dataset.arg0),
  selectFingerprint: (node)=>window.FingerprintUI?.select(node.dataset.arg0),
  showFingerprintSample: node=>window.FingerprintUI?.showSample(Number(node.dataset.arg0)),
  selectQuality: (node)=>selectQuality(node.dataset.arg0),
  selectRouting: (node)=>selectRouting(node.dataset.arg0),
  selectTask: (node)=>selectTask(node.dataset.arg0),
  setComparisonTab: (node)=>setComparisonTab(node.dataset.arg0,node.dataset.arg1),
  setFingerprintTab: (node)=>window.FingerprintUI?.setTab(node.dataset.arg1),
  setQualityTab: (node)=>setQualityTab(node.dataset.arg0,node.dataset.arg1),
  setGatewayRequestFilter: node=>setGatewayRequestFilter(node.dataset.arg0,node.dataset.arg1),
  setRecordFilter: (node)=>setRecordFilter(node.dataset.arg0),
  setRoutingTab: (node)=>setRoutingTab(node.dataset.arg0,node.dataset.arg1),
  setRoutingRequestsPage: (node)=>setRoutingRequestsPage(node.dataset.arg0,node.dataset.arg1,node.dataset.arg2),
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
// Keep utilities visible when all six modules share a narrow horizontal navigation.
// Move the existing nodes so theme state, focus and event handlers stay attached.
const compactNavigation=window.matchMedia('(max-width:980px)');
function arrangeNavigationTools(){
  const tools=document.querySelector('.rail-tools');
  const host=document.querySelector(compactNavigation.matches?'.top':'.sidebar');
  if(!tools||!host)return;
  const focused=document.activeElement;
  if(compactNavigation.matches)host.insertBefore(tools,host.querySelector('.account-tools'));
  else host.appendChild(tools);
  if(tools.contains(focused))focused.focus({preventScroll:true});
}
compactNavigation.addEventListener('change',arrangeNavigationTools);
arrangeNavigationTools();
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
