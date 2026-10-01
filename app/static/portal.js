// Small behaviours. Kept in a file (not inline) so the Content-Security-Policy can block inline scripts.
(function () {
  // Lets CSS hide the no-JavaScript fallbacks (like "Show times") once scripts are running.
  document.documentElement.classList.add("js");

  // Ask before destructive form posts: <form data-confirm="Delete this?">
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.getAttribute("data-confirm"))) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  // Copy buttons: <button data-copy="#input-id">
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var src = document.querySelector(btn.getAttribute("data-copy"));
    if (!src) return;
    var text = src.value || src.textContent;
    navigator.clipboard.writeText(text.trim()).then(function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1600);
    });
  });

  // Clear the reply form after htmx posts it, but only when the server says it went through
  // (so a rejected attachment doesn't wipe what the person typed).
  document.addEventListener("htmx:afterRequest", function (e) {
    var form = e.detail.elt;
    var xhr = e.detail.xhr;
    if (form && form.matches && form.matches("form[data-reset-on-success]") && e.detail.successful &&
        xhr && xhr.getResponseHeader("X-Form-Reset") === "1") {
      form.reset();
      var first = form.querySelector("textarea, input:not([type=hidden]):not([type=file])");
      if (first) first.focus();
    }
  });

  // Print button on invoices.
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-print]")) window.print();
  });
})();
