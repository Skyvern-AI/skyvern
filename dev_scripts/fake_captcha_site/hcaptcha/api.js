// Local stand-in for https://js.hcaptcha.com/1/api.js in invisible mode. Measured from the real script before
// execute(): .h-captcha is 1264x0, the checkbox iframe display:none, the challenge iframe hidden at y=-9999.
(function () {
  var widget = document.querySelector(".h-captcha");
  if (!widget) {
    return;
  }
  var siteKey = widget.getAttribute("data-sitekey");
  var callbackName = widget.getAttribute("data-callback");

  var checkboxFrame = document.createElement("iframe");
  checkboxFrame.setAttribute("aria-hidden", "true");
  checkboxFrame.setAttribute("data-hcaptcha-response", "");
  checkboxFrame.src = "hcaptcha_widget.html#frame=checkbox-invisible";
  checkboxFrame.style.display = "none";
  widget.appendChild(checkboxFrame);

  var response = document.createElement("textarea");
  response.name = "h-captcha-response";
  response.id = "h-captcha-response";
  response.style.display = "none";
  widget.appendChild(response);

  var legacyResponse = document.createElement("textarea");
  legacyResponse.name = "g-recaptcha-response";
  legacyResponse.style.display = "none";
  widget.appendChild(legacyResponse);

  var challengeFrame = document.createElement("iframe");
  challengeFrame.src = "hcaptcha_widget.html#frame=challenge";
  challengeFrame.style.cssText =
    "position:absolute;left:9px;top:-9999px;width:300px;height:150px;border:0;visibility:hidden;";
  document.body.appendChild(challengeFrame);

  window.hcaptcha = {
    execute: function () {
      return new Promise(function (resolve) {
        setTimeout(function () {
          var token = "fixture-invisible-token." + siteKey;
          response.value = token;
          legacyResponse.value = token;
          if (callbackName && typeof window[callbackName] === "function") {
            window[callbackName](token);
          }
          resolve({ response: token });
        }, 300);
      });
    },
    getResponse: function () {
      return response.value;
    },
  };
})();
