// Local stand-in for https://www.google.com/recaptcha/api.js?render=<sitekey>. It mints a score
// token after a short delay and never escalates to a userverify/bframe interactive challenge,
// which is the network shape this fixture exists to reproduce.
window.grecaptcha = {
  ready: function (callback) {
    setTimeout(callback, 250);
  },
  execute: function (siteKey, options) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(
          "fixture-v3-token." + siteKey + "." + (options && options.action),
        );
      }, 1200);
    });
  },
};
