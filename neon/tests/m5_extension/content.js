// Experiment E1 of neon/docs/global-space-design.md: what a content script sees
// in a container tab. The test reads the results from the page's DOM.
const root = document.documentElement;
root.dataset.neonCsCookie = document.cookie;
fetch('/echo', {credentials: 'include'})
    .then(r => r.text())
    .then(t => { root.dataset.neonCsFetch = t; })
    .catch(e => { root.dataset.neonCsFetch = 'error ' + e; });
