// Generic click-to-sort for <table class="sortable">. Add data-num to a <th>
// for numeric sorting; otherwise sorts as text. Clicking again reverses.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('table.sortable').forEach(function (table) {
    var headers = table.querySelectorAll('thead th');
    headers.forEach(function (th, idx) {
      th.style.cursor = 'pointer';
      th.dataset.dir = '';
      th.addEventListener('click', function () {
        var tbody = table.querySelector('tbody');
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
        headers.forEach(function (h) { h.dataset.dir = ''; h.classList.remove('sorted-asc', 'sorted-desc'); });
        th.dataset.dir = dir;
        th.classList.add(dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
        var isNum = th.hasAttribute('data-num');
        rows.sort(function (a, b) {
          var av = a.children[idx] ? a.children[idx].textContent.trim() : '';
          var bv = b.children[idx] ? b.children[idx].textContent.trim() : '';
          if (isNum) {
            av = parseFloat(av.replace(/[^0-9.\-]/g, '')); if (isNaN(av)) av = -Infinity;
            bv = parseFloat(bv.replace(/[^0-9.\-]/g, '')); if (isNaN(bv)) bv = -Infinity;
            return dir === 'asc' ? av - bv : bv - av;
          }
          av = av.toLowerCase(); bv = bv.toLowerCase();
          if (av < bv) return dir === 'asc' ? -1 : 1;
          if (av > bv) return dir === 'asc' ? 1 : -1;
          return 0;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  });
});
