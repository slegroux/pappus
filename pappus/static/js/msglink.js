
(function(){
  function scrollToCell(mid){
    var el = document.getElementById('cell-'+mid);
    if(!el) return false;
    el.scrollIntoView({behavior:'smooth', block:'center'});
    el.classList.remove('msg-flash'); void el.offsetWidth;   // restart the flash animation
    el.classList.add('msg-flash');
    setTimeout(function(){ el.classList.remove('msg-flash'); }, 1600);
    return true;
  }
  window.__scrollToCell = scrollToCell;
  // A #_msgid link lives inside a note, which is itself click-to-edit (htmx
  // hx-get on click). Capture the click BEFORE it bubbles to that edit trigger
  // and stop it there, so following a link never drops the cell into edit mode.
  // Same-dialog links scroll in place; cross-dialog (.xdlg) links keep their
  // href so they open the other dialog.
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('a.msglink');
    if(!a) return;
    e.stopPropagation();                       // don't trip the cell's click-to-edit
    if(a.classList.contains('xdlg')) return;   // let it navigate to the other dialog
    var mid = a.getAttribute('data-mid');
    if(mid && document.getElementById('cell-'+mid)){ e.preventDefault(); scrollToCell(mid); }
  }, true);
  // Copy a cell's #_msgid anchor — the same string you paste inline to link here.
  // Brief tooltip feedback so the click lands visibly.
  window._copyMsgLink = function(btn){
    var ref = btn.getAttribute('data-anchor');
    function ok(){ var t = btn.getAttribute('title'); btn.setAttribute('title','Copied '+ref);
      btn.classList.add('copied');
      setTimeout(function(){ btn.setAttribute('title', t); btn.classList.remove('copied'); }, 1400); }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(ref).then(ok, ok);
    } else {
      var ta = document.createElement('textarea'); ta.value = ref; document.body.appendChild(ta);
      ta.select(); try{ document.execCommand('copy'); }catch(e){} ta.remove(); ok();
    }
  };
  // Honor a #_msgid in the URL on load (e.g. after /open?dialog=…#_id) and on
  // manual hash edits — but NOT on htmx swaps, which shouldn't yank the view.
  function honorHash(){
    var m = (location.hash || '').match(/^#(_[0-9a-f]{6,})$/);
    if(m) requestAnimationFrame(function(){ scrollToCell(m[1]); });
  }
  window.addEventListener('DOMContentLoaded', honorHash);
  window.addEventListener('hashchange', honorHash);
  honorHash();
})();
