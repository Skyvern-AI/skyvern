// custom.js
(function () {
  var SRC = "https://cosjk17vk.skyvern.com/analytics-0.1.js";
  var ID = "customeros-metrics-loader";

  function inject() {
    if (
      !document.getElementById(ID) &&
      !document.querySelector('script[src="' + SRC + '"]')
    ) {
      var s = document.createElement("script");
      s.id = ID;
      s.async = true;
      s.src = SRC;
      (document.head || document.body).appendChild(s);
    }
  }

  // Inject the analytics script on page load
  document.readyState === "loading"
    ? document.addEventListener("DOMContentLoaded", inject)
    : inject();
})();
