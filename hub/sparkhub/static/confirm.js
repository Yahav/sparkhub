// Forms with data-confirm ask before they submit (no inline handlers: the CSP allows scripts from /static only).
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
