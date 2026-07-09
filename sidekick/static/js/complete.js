
window.__kernelHint = function(cm, callback){
  var cur = cm.getCursor(), line = cm.getLine(cur.line);
  var startCh = cur.ch;                                   // start of the typed identifier
  while(startCh && /[A-Za-z0-9_]/.test(line.charAt(startCh - 1))) startCh--;
  var body = 'code=' + encodeURIComponent(cm.getValue()) +
             '&line=' + (cur.line + 1) + '&col=' + cur.ch;
  fetch('/complete', {method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body})
    .then(function(r){ return r.json(); })
    .then(function(data){
      var comps = (data && data.completions) || [];
      if(!comps.length){ callback(null); return; }
      callback({
        list: comps.map(function(c){ return {text: c.name, displayText: c.name}; }),
        from: CodeMirror.Pos(cur.line, startCh),
        to: CodeMirror.Pos(cur.line, cur.ch)
      });
    })
    .catch(function(){ callback(null); });   // never break typing
};
window.__kernelHint.async = true;            // tells CodeMirror it uses a callback

// Open the completion dropdown. Tab (and Enter) accept the highlighted item —
// the SolveIt convention. completeSingle:false so a lone match never auto-inserts
// while you're mid-word.
window.__showCompletions = function(cm){
  if(!window.__kernelHint) return;
  cm.showHint({
    hint: window.__kernelHint,
    completeSingle: false,
    extraKeys: { 'Tab': function(cm, handle){ handle.pick(); } }
  });
};

// Auto-trigger as you type (SolveIt's "dynamic autocomplete"). On a typed word
// char or a '.', open the dropdown after a short debounce — unless one is already
// open (it updates itself). Programmatic edits (e.g. accepting a hint) don't fire
// inputRead, so this never loops. Debounced to stay light over the H100 tunnel.
window.__autocompleteOnType = function(cm){
  cm.on('inputRead', function(cm, change){
    if(cm.state.completionActive) return;             // already open → it self-updates
    var ch = change.text && change.text[0];
    if(!ch || !/[\w.]/.test(ch)) return;             // only identifier chars and '.'
    clearTimeout(cm.__hintTimer);
    cm.__hintTimer = setTimeout(function(){
      if(!cm.state.completionActive) window.__showCompletions(cm);
    }, 160);
  });
};
