
(function(){
  if(window.__streamSel) return; window.__streamSel = true;
  var bar = null, curText = '';
  function hide(){ if(bar) bar.style.display = 'none'; }
  document.addEventListener('mouseup', function(ev){
    if(bar && ev.target && bar.contains(ev.target)) return;       // a click on the bubble itself
    var stream = document.getElementById('stream');
    var sel = window.getSelection();
    var text = sel ? sel.toString().trim() : '';
    if(!stream || !text || !sel.anchorNode || !stream.contains(sel.anchorNode)){ hide(); return; }
    // Don't intrude while editing a cell — CodeMirror / the textarea own their selection UX.
    var n = sel.anchorNode, el = n && (n.nodeType === 3 ? n.parentElement : n);
    if(el && el.closest && el.closest('.CodeMirror, .cell-edit')){ hide(); return; }
    if(!bar){
      bar = document.createElement('div'); bar.className = 'sel-tools';
      var b = document.createElement('button');
      b.className = 'sel-btn import'; b.textContent = 'Ask AI ↗';
      b.title = 'Drop the selected text into the composer as an Ask-AI question';
      b.addEventListener('mousedown', function(e){
        e.preventDefault();                                        // keep the selection alive
        if(window.__askComposer) window.__askComposer(curText);
        hide();
      });
      bar.appendChild(b);
      document.body.appendChild(bar);
    }
    curText = text;
    var r = sel.getRangeAt(0).getBoundingClientRect();
    bar.style.top = (window.scrollY + r.bottom + 6) + 'px';
    bar.style.left = (window.scrollX + r.left) + 'px';
    bar.style.display = 'flex';
  });
})();
