
var composerCM = null;   // CodeMirror instance while the composer is in Code mode

// Drop an immediate "Thinking…" wheel into the stream the instant you send an
// Ask-AI prompt — before any server round-trip. The htmx response then swaps all
// of #stream and replaces it: with the streaming answer (Claude Max) or the final
// answer (blocking API models, which otherwise showed no indicator at all). So a
// wheel is always visible while you wait, regardless of model or speed.
function _showPendingSpinner(){
  var stream = document.getElementById('stream');
  if(!stream || document.getElementById('pending-spinner')) return;
  var box = stream.querySelector('.wrap') || stream;
  var d = document.createElement('div');
  d.id = 'pending-spinner'; d.className = 'row';
  d.innerHTML = '<div class="answer"><div class="bubble md"><span class="thinking">' +
                '<span class="spinner"></span>Thinking…</span></div></div>';
  box.appendChild(d);
  stream.scrollTop = stream.scrollHeight;
}
// Re-running an existing Ask-AI cell posts to the server and then swaps #stream,
// but until that lands the stale answer just sits there. If the first streamed
// token arrives quickly the server-rendered spinner only flashes, so a re-ask
// reads as "no spinner". Drop a wheel into the cell's answer bubble the instant
// you click — the same optimistic feedback the composer gets. The #stream swap
// then renders its own identical spinner, so the handoff is seamless.
function _showCellSpinner(id){
  var cell = document.getElementById('cell-' + id);
  if(!cell) return;
  var spin = '<span class="thinking"><span class="spinner"></span>Thinking…</span>';
  var bubble = cell.querySelector('.answer .bubble');
  if(bubble){ bubble.className = 'bubble md'; bubble.innerHTML = spin; return; }
  var ans = document.createElement('div');     // never-answered prompt: add a bubble
  ans.className = 'answer';
  ans.innerHTML = '<div class="bubble md">' + spin + '</div>';
  cell.appendChild(ans);
}
function _submitComposer(){
  if(composerCM) composerCM.save();                 // flush editor -> textarea
  var ta = document.getElementById('composerInput');
  if(!ta || !ta.value.trim()) return;
  var isPrompt = (document.getElementById('msgType').value === 'prompt');
  // requestSubmit() fires the submit event so htmx posts and swaps just #stream
  // (no full-page reload). htmx serializes the form synchronously, so it's safe
  // to clear the composer right after.
  document.getElementById('composerForm').requestSubmit();
  if(isPrompt) _showPendingSpinner();               // instant feedback until the swap lands
  ta.value = '';
  if(composerCM) composerCM.setValue('');
}
// A successful /send swaps #stream and so removes the optimistic spinner; but if
// the request errors (no swap), clear the stray wheel so it can't hang forever.
if(!window.__pendingSpinnerCleanup){
  window.__pendingSpinnerCleanup = true;
  document.addEventListener('htmx:afterRequest', function(){
    var s = document.getElementById('pending-spinner'); if(s) s.remove();
  });
}
function _initComposerCM(){
  var ta = document.getElementById('composerInput');
  if(!ta || composerCM || !window.CodeMirror) return;   // offline: stays a plain textarea
  composerCM = CodeMirror.fromTextArea(ta, {
    mode: 'python', theme: 'monokai', lineNumbers: false,
    viewportMargin: Infinity, indentUnit: 4, lineWrapping: true,
    placeholder: '# code…  Shift+Enter to run · Enter for newline · Tab switches mode',
    extraKeys: {
      'Shift-Enter': _submitComposer, 'Cmd-Enter': _submitComposer, 'Ctrl-Enter': _submitComposer,
      'Cmd-/': function(){ window.__toggleComment(composerCM); },
      'Ctrl-/': function(){ window.__toggleComment(composerCM); },
      'Tab': function(){ cycleMode(1); }, 'Shift-Tab': function(){ cycleMode(-1); }
    }
  });
  composerCM.on('change', function(){ composerCM.save(); });
  setTimeout(function(){ composerCM.refresh(); composerCM.focus(); }, 0);
}
function _destroyComposerCM(){
  if(!composerCM) return;
  composerCM.save();
  composerCM.toTextArea();                            // restore the plain textarea
  composerCM = null;
  var ta = document.getElementById('composerInput'); if(ta) ta.focus();
}
function setMode(v){
  document.getElementById('msgType').value = v;
  document.querySelectorAll('#modeChips .mode').forEach(function(el){
    el.classList.toggle('sel', el.getAttribute('data-val') === v);
  });
  if(v === 'code') _initComposerCM(); else _destroyComposerCM();
}
var COMPOSER_MODES = ['prompt','code','note'];   // order matches the chips: Ask AI / Code / Note
function cycleMode(dir){
  var i = COMPOSER_MODES.indexOf(document.getElementById('msgType').value);
  if(i < 0) i = 0;
  setMode(COMPOSER_MODES[(i + dir + COMPOSER_MODES.length) % COMPOSER_MODES.length]);
}
// Shared by the paper-panel toolbar and the dialog-stream selection bubble: drop a
// quoted passage into the composer as an Ask-AI prompt (switches to prompt mode,
// which also tears down the Code editor so the textarea holds the quote).
window.__askComposer = function(text){
  setMode('prompt');
  var ta = document.getElementById('composerInput');
  if(!ta) return;
  var quote = String(text || '').split('\n').map(function(l){ return '> ' + l; }).join('\n');
  ta.value = quote + '\n\n';
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  ta.scrollIntoView({block: 'center'});
};
(function(){
  var ta = document.getElementById('composerInput');
  if(!ta) return;
  // Plain-textarea keys (Ask AI / Note, and the offline Code fallback).
  ta.addEventListener('keydown', function(e){
    if(e.key === 'Tab'){                 // Tab cycles Ask AI -> Code -> Note (Shift+Tab back)
      e.preventDefault();
      cycleMode(e.shiftKey ? -1 : 1);
      return;
    }
    if(e.key === 'Enter'){
      // One notebook convention for every cell type: plain Enter = newline,
      // Shift/Cmd/Ctrl+Enter = run/send (Ask AI, Code, and Note all match).
      if(e.shiftKey || e.metaKey || e.ctrlKey){ e.preventDefault(); _submitComposer(); }
    }
  });
  if(document.getElementById('msgType').value === 'code') _initComposerCM();  // sticky Code mode
  else ta.focus();
})();
