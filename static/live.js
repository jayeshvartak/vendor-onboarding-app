(function () {
  var scrollKey = "vo-live:" + location.pathname + location.search;
  var saved = sessionStorage.getItem(scrollKey);
  if (saved) {
    sessionStorage.removeItem(scrollKey);
    var y = Number(saved);
    if (!Number.isNaN(y)) {
      requestAnimationFrame(function () { window.scrollTo(0, y); });
    }
  }

  var revision = null;
  var pending = false;

  function editing() {
    var el = document.activeElement;
    if (!el || !el.closest) return false;
    var tag = el.tagName;
    return !!el.closest("form") && (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT");
  }

  function dirty() {
    var fields = document.querySelectorAll("form input, form textarea, form select");
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (field.type === "hidden" || field.type === "submit" || field.type === "button") continue;
      if (field.tagName === "SELECT") {
        var option = field.querySelector("option[selected]");
        var initial = option ? option.value : "";
        if (field.value !== initial) return true;
      } else if (field.type === "checkbox" || field.type === "radio") {
        if (field.checked !== field.defaultChecked) return true;
      } else if (field.value !== field.defaultValue) {
        return true;
      }
    }
    return false;
  }

  function reload() {
    sessionStorage.setItem(scrollKey, String(window.scrollY || 0));
    location.reload();
  }

  function note() {
    if (document.getElementById("live-refresh")) return;
    var bar = document.createElement("div");
    bar.id = "live-refresh";
    bar.className = "flash ok";
    bar.innerHTML = "This page is out of date. <button type=\"button\" class=\"btn\" id=\"live-refresh-now\">Refresh</button>";
    var main = document.querySelector("main");
    if (!main) return;
    main.insertBefore(bar, main.firstChild);
    document.getElementById("live-refresh-now").addEventListener("click", reload);
  }

  function tick() {
    fetch("/live.json", { cache: "no-store" }).then(function (res) {
      if (!res.ok) return null;
      return res.json();
    }).then(function (body) {
      if (!body) return;
      if (revision === null) {
        revision = body.revision;
        return;
      }
      if (body.revision !== revision) pending = true;
      if (!pending) return;
      if (editing() || dirty()) {
        note();
        return;
      }
      reload();
    }).catch(function () {});
  }

  setInterval(tick, 2000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
})();
