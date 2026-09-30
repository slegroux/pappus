
window.__toggleComment = function(cm){
  cm.operation(function(){
    cm.listSelections().forEach(function(sel){
      var from = Math.min(sel.anchor.line, sel.head.line);
      var to   = Math.max(sel.anchor.line, sel.head.line);
      var lines = [];
      for(var i = from; i <= to; i++){ if(/\S/.test(cm.getLine(i))) lines.push(i); }
      if(!lines.length) lines = [from];                 // act on a lone blank line too
      var commented = lines.every(function(i){ return /^\s*#/.test(cm.getLine(i)); });
      var indent = Infinity;
      lines.forEach(function(i){ indent = Math.min(indent, cm.getLine(i).match(/^\s*/)[0].length); });
      if(!isFinite(indent)) indent = 0;
      lines.forEach(function(i){
        if(commented){
          var m = cm.getLine(i).match(/^(\s*)#( ?)/);  // strip the leading '# ' (or '#')
          if(m) cm.replaceRange('', {line:i, ch:m[1].length}, {line:i, ch:m[1].length + 1 + m[2].length});
        } else {
          cm.replaceRange('# ', {line:i, ch:indent});
        }
      });
    });
  });
};
