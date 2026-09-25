(function () {
  try {
    var hostname = window.location.hostname;
    if (hostname !== "skyvern.com" && !hostname.endsWith(".skyvern.com"))
      return;
    if (window.__skyvernDocsPostHogLoaded) return;

    var existing = window.posthog;
    if (
      existing &&
      !(
        Array.isArray(existing) &&
        existing.__SV === 1 &&
        Array.isArray(existing._i) &&
        typeof existing.init === "function"
      )
    )
      return;
    window.__skyvernDocsPostHogLoaded = true;

    var KEY = "phc_m4epBbGS1Hf4NPRFNpR4WQ9Ob6yGy6SLbQckBxp3n0P";
    var UTM_KEYS = [
      "utm_source",
      "utm_medium",
      "utm_campaign",
      "utm_term",
      "utm_content",
    ];
    var PUBLIC_PATHS = new Set([
      "/",
      "/products",
      "/developers",
      "/use-cases",
      "/healthcare",
      "/insurance",
      "/fintech",
      "/hr-tech",
      "/pricing",
      "/integrations",
      "/llms",
      "/contact",
      "/privacy",
      "/terms",
      "/blog",
    ]);
    var PRIVATE_QUERY_FIELDS = new Set([
      "gclid",
      "gclsrc",
      "dclid",
      "gbraid",
      "wbraid",
      "fbclid",
      "msclkid",
      "twclid",
      "li_fat_id",
      "igshid",
      "ttclid",
      "rdt_cid",
      "epik",
      "qclid",
      "sccid",
      "irclid",
      "_kx",
      "gad_source",
      "mc_cid",
      "utm_id",
      "utm_source_platform",
      "bfcid",
      "ph_keyword",
      "skyvern_referrer",
      "skyvern_landing_path",
    ]);

    // Keep consent and sanitization aligned with landing_page/src/lib/cookie-consent.ts, landing_page/src/lib/script-loader.ts, and landing_page/src/lib/first-touch.ts.
    function hasAnalyticsConsent() {
      try {
        var name = "skyvern_cookie_consent=";
        var cookies = document.cookie.split(";").map(function (cookie) {
          return cookie.trim();
        });
        var choices = cookies
          .filter(function (cookie) {
            return cookie.startsWith(name);
          })
          .map(function (cookie) {
            return JSON.parse(decodeURIComponent(cookie.slice(name.length)));
          });
        if (choices.length === 0) {
          var regionName = "skyvern_consent_region=";
          var regions = cookies.filter(function (cookie) {
            return cookie.startsWith(regionName);
          });
          return (
            regions.length > 0 &&
            regions.every(function (cookie) {
              return cookie.slice(regionName.length) === "opt_out";
            })
          );
        }
        return (
          choices.length > 0 &&
          choices.every(function (choice) {
            return (
              choice &&
              typeof choice === "object" &&
              !Array.isArray(choice) &&
              choice.analytics === true
            );
          })
        );
      } catch {
        return null;
      }
    }

    function expireCookie(key, domain) {
      try {
        document.cookie =
          key +
          "=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/" +
          (domain ? ";domain=" + domain : "") +
          ";SameSite=Lax";
      } catch {}
    }

    function purgeSessionStorage() {
      for (var key of ["ph_" + KEY + "_posthog", "ph_" + KEY + "_window_id"]) {
        try {
          sessionStorage.removeItem(key);
        } catch {}
      }
    }

    function purgeStaleConsent() {
      var optInOutKey = "__ph_opt_in_out_" + KEY;
      var identityKey = "ph_" + KEY + "_posthog";
      var cookieDomain = "." + hostname.split(".").slice(-2).join(".");
      var cookieOptInOutValue;
      try {
        var cookies = document.cookie.split(";");
        for (var i = 0; i < cookies.length; i++) {
          var entry = cookies[i].trim();
          if (entry.startsWith(optInOutKey + "=")) {
            cookieOptInOutValue = entry.slice(optInOutKey.length + 1);
            break;
          }
        }
      } catch {}
      // Preserve refusals; a stale grant must not restore a stored identity before consent.
      if (cookieOptInOutValue !== "0") {
        expireCookie(optInOutKey);
        expireCookie(optInOutKey, cookieDomain);
      }
      expireCookie(identityKey);
      expireCookie(identityKey, cookieDomain);

      var localStorageOptInOutValue = null;
      try {
        localStorageOptInOutValue = localStorage.getItem(optInOutKey);
      } catch {}
      if (localStorageOptInOutValue !== "0") {
        try {
          localStorage.removeItem(optInOutKey);
        } catch {}
      }
      try {
        localStorage.removeItem(identityKey);
      } catch {}
      purgeSessionStorage();
    }

    function withdrawAnalyticsConsent(posthog) {
      var originalSessionPersistence;
      try {
        originalSessionPersistence = posthog.sessionPersistence;
      } catch {}
      try {
        posthog.set_config({
          persistence: "memory",
          disable_persistence: true,
        });
        restorePersistence = true;
      } catch {}
      try {
        posthog.persistence?.clear();
      } catch {}
      try {
        originalSessionPersistence?.clear();
      } catch {}
      var cookieDomain = "." + hostname.split(".").slice(-2).join(".");
      for (var key of ["__ph_opt_in_out_" + KEY, "ph_" + KEY + "_posthog"]) {
        try {
          localStorage.removeItem(key);
        } catch {}
        expireCookie(key);
        expireCookie(key, cookieDomain);
      }
      purgeSessionStorage();
      try {
        posthog.opt_out_capturing();
      } catch {}
    }

    function safeUtm(key, raw) {
      if (typeof raw !== "string") return;
      var value = raw.trim();
      var safeValue = /^[A-Za-z0-9+](?:[A-Za-z0-9 ._+~-]*[A-Za-z0-9._+~-])?$/;
      var tokenValue =
        /^(?:bearer[ ._+-]|(?:sk|pk|rk)_(?:live|test)_|(?:gh[opusr]|github_pat)_|eyJ[A-Za-z0-9_-]*\.)/i;
      if (
        !value ||
        value.length > 128 ||
        !safeValue.test(value) ||
        tokenValue.test(value)
      )
        return;
      if (/^\d+$/.test(value)) {
        return (key === "utm_campaign" || key === "utm_content") &&
          value.length <= 32
          ? value
          : undefined;
      }
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value) && /^\+?[\d -]{7,}$/.test(value))
        return;
      return value;
    }

    // Mirror the site parseFirstTouchUtm event rules without persisting attribution.
    function parsePageUtm(search) {
      try {
        var params = new URLSearchParams(search);
        var properties = {};
        for (var key of UTM_KEYS) {
          var value =
            params.getAll(key).length === 1
              ? safeUtm(key, params.get(key))
              : undefined;
          if (value) properties[key] = value;
        }
        // Infer channels from valid click IDs without retaining their values.
        var clickIds = params.getAll("bfcid");
        var clickId = clickIds.length === 1 ? clickIds[0] : undefined;
        if (
          !params.has("utm_source") &&
          clickId &&
          clickId.length <= 512 &&
          /^bfc_1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(clickId)
        ) {
          properties.utm_source = "freebuff";
        }
        var networks = new Set();
        for (var [key, network] of Object.entries({
          gclid: "google",
          gbraid: "google",
          wbraid: "google",
          dclid: "google",
          msclkid: "microsoft",
          rdt_cid: "reddit",
          li_fat_id: "linkedin",
          twclid: "x",
          ttclid: "tiktok",
          epik: "pinterest",
          sccid: "snapchat",
          qclid: "quora",
        })) {
          var values = params.getAll(key);
          if (values.length === 0) continue;
          var value = values.length === 1 ? values[0] : undefined;
          if (!value || value.length > 512 || !/^[A-Za-z0-9._~-]+$/.test(value))
            return properties;
          networks.add(network);
        }
        if (networks.size === 1) {
          var medium = (properties.utm_medium ?? "")
            .toLowerCase()
            .replace(/[ -]/g, "_");
          if (
            !/^(?:paid[a-z0-9_-]*|cpc|ppc|cpm|cpv|display|ads?|sponsored|sem)$/.test(
              medium,
            )
          ) {
            properties.utm_medium = "paid";
          }
          if (!params.has("utm_source"))
            properties.utm_source ??= [...networks][0];
        }
        return properties;
      } catch {
        return {};
      }
    }

    function safeDomain(raw) {
      if (
        typeof raw !== "string" ||
        raw.length > 253 ||
        !/^[A-Za-z0-9.-]+$/.test(raw)
      )
        return;
      var value = raw.toLowerCase();
      if (
        !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(
          value,
        ) ||
        value === "skyvern.com" ||
        value.endsWith(".skyvern.com") ||
        value.endsWith(".localhost")
      )
        return;
      return value;
    }

    function referringDomain(raw) {
      try {
        var url = new URL(raw);
        if (/^https?:$/.test(url.protocol) && !url.username && !url.password)
          return safeDomain(url.hostname);
      } catch {}
    }

    function attributionLandingPath(raw) {
      try {
        if (typeof raw !== "string" || raw.length > 256) return;
        var path = raw.split(/[?#]/, 1)[0];
        if (path.startsWith("//")) return;
        var normalized = path === "/" ? path : path.replace(/\/$/, "");
        if (
          PUBLIC_PATHS.has(normalized) ||
          /^\/blog\/(?!preview\/?$)[a-z0-9]+(?:-[a-z0-9]+)*\/?$/.test(path)
        ) {
          return path;
        }
      } catch {}
    }

    function docsPath(value) {
      if (typeof value !== "string") return;
      var path = value.split(/[?#]/, 1)[0];
      if (path === "/docs" || path.startsWith("/docs/")) return path;
    }

    function publicUrl(value) {
      if (typeof value !== "string") return;
      try {
        var url = new URL(value);
        if (
          url.protocol === "https:" &&
          (url.hostname === "skyvern.com" ||
            url.hostname === "www.skyvern.com") &&
          docsPath(url.pathname)
        ) {
          return url.origin + url.pathname;
        }
      } catch {}
    }

    var trimmedInfoCache = new WeakMap();
    var guardedPersistence = new WeakSet();
    var guardedInstances = new WeakSet();

    function trimmedStoredPersonInfo(info) {
      try {
        if (!info || typeof info !== "object" || Array.isArray(info))
          return info;
        var cached = trimmedInfoCache.get(info);
        if (cached && cached.u === info.u && cached.r === info.r)
          return cached.value;
        var value = info;
        if (typeof info.u === "string") {
          var url = new URL(info.u);
          if (
            url.origin === "https://skyvern.com" ||
            url.origin === "https://www.skyvern.com"
          ) {
            // Mirror first-touch.ts trimPostHogStoredInfo, including literal query segments.
            var path =
              attributionLandingPath(url.pathname) || docsPath(url.pathname);
            var query = url.search
              .slice(1)
              .split("&")
              .filter(function (part) {
                var entry = new URLSearchParams(part).entries().next().value;
                if (!entry) return false;
                var key = entry[0];
                return (
                  UTM_KEYS.includes(key) &&
                  url.searchParams.getAll(key).length === 1 &&
                  safeUtm(key, entry[1]) !== undefined
                );
              })
              .join("&");
            var u = path
              ? url.origin + path + (query ? "?" + query : "")
              : undefined;
            var domain =
              typeof info.r === "string"
                ? safeDomain(info.r) || referringDomain(info.r)
                : undefined;
            var r = domain ? "https://" + domain : "$direct";
            if (u !== info.u || r !== info.r) value = { ...info, u: u, r: r };
          }
        }
        trimmedInfoCache.set(info, { u: info.u, r: info.r, value: value });
        return value;
      } catch {
        return info;
      }
    }

    function trimmedSessionEntry(entry) {
      try {
        if (
          !entry ||
          typeof entry !== "object" ||
          Array.isArray(entry) ||
          typeof entry.sessionId !== "string"
        )
          return entry;
        var props = trimmedStoredPersonInfo(entry.props);
        return props === entry.props ? entry : { ...entry, props: props };
      } catch {
        return entry;
      }
    }

    function trimStoredProperties(properties) {
      try {
        if (
          !properties ||
          typeof properties !== "object" ||
          ![
            "ph_keyword",
            "$client_session_props",
            "$initial_person_info",
            "$referrer",
            "$referring_domain",
          ].some(function (key) {
            return key in properties;
          })
        )
          return properties;
        var safe = { ...properties };
        delete safe.ph_keyword;
        if ("$client_session_props" in safe)
          safe.$client_session_props = trimmedSessionEntry(
            safe.$client_session_props,
          );
        if ("$initial_person_info" in safe)
          safe.$initial_person_info = trimmedStoredPersonInfo(
            safe.$initial_person_info,
          );
        if ("$referrer" in safe || "$referring_domain" in safe) {
          var raw =
            "$referrer" in safe ? safe.$referrer : safe.$referring_domain;
          var domain =
            typeof raw === "string"
              ? safeDomain(raw) || referringDomain(raw)
              : undefined;
          safe.$referrer = domain || "$direct";
          safe.$referring_domain = domain || "$direct";
        }
        return safe;
      } catch {
        return {};
      }
    }

    function guardPersistenceWrites(posthog) {
      try {
        // PostHog 1.434.10: session-props.ts:60-84 writes via register; core.ts:4127 replaces sessionPersistence.
        if (!guardedInstances.has(posthog)) {
          var setConfig = posthog.set_config;
          posthog.set_config = function () {
            try {
              return setConfig.apply(this, arguments);
            } finally {
              try {
                guardPersistenceWrites(this);
              } catch {}
            }
          };
          var capture = posthog.capture;
          if (typeof capture === "function") {
            posthog.capture = function () {
              try {
                if (
                  (restorePersistence ||
                    this.config?.persistence === "memory" ||
                    this.config?.disable_persistence === true) &&
                  hasAnalyticsConsent() === true &&
                  this.has_opted_in_capturing()
                ) {
                  var shared = readSharedPersistence();
                  if (shared && !restorePersistentCapture(this, shared)) return;
                }
                return capture.apply(this, arguments);
              } catch {}
            };
          }
          guardedInstances.add(posthog);
        }
        [posthog.persistence, posthog.sessionPersistence].forEach(
          function (store) {
            try {
              if (!store || guardedPersistence.has(store)) return;
              ["register", "register_once"].forEach(function (method) {
                try {
                  var original = store[method];
                  if (typeof original !== "function") return;
                  store[method] = function (properties) {
                    try {
                      var args = Array.from(arguments);
                      args[0] = trimStoredProperties(properties);
                      return original.apply(this, args);
                    } catch {
                      return false;
                    }
                  };
                } catch {}
              });
              guardedPersistence.add(store);
              if (hasAnalyticsConsent() === true && store.props) {
                var safe = trimStoredProperties(store.props);
                [
                  "$initial_person_info",
                  "$client_session_props",
                  "$referrer",
                  "$referring_domain",
                ].forEach(function (key) {
                  try {
                    if (safe[key] !== store.props[key])
                      store.register?.({ [key]: safe[key] });
                  } catch {}
                });
                if ("ph_keyword" in store.props)
                  store.unregister?.("ph_keyword");
              }
            } catch {}
          },
        );
      } catch {}
    }

    function scrubSessionEntryProps(posthog) {
      try {
        if (hasAnalyticsConsent() !== true) return;
        var stored = posthog?.persistence?.props;
        if (!stored) return;
        var initial = stored.$initial_person_info;
        var safeInitial = trimmedStoredPersonInfo(initial);
        if (safeInitial !== initial)
          posthog.register({ $initial_person_info: safeInitial });
        var entry = stored.$client_session_props;
        var safeEntry = trimmedSessionEntry(entry);
        if (safeEntry !== entry)
          posthog.register({ $client_session_props: safeEntry });
      } catch {}
    }

    function sanitizeProperties(properties) {
      var original = { ...properties };
      for (var key of Object.keys(original)) {
        var value = original[key];
        if (
          (key === "$set" || key === "$set_once") &&
          value &&
          typeof value === "object"
        ) {
          sanitizeProperties(value);
          continue;
        }
        var field = key
          .replace(/^\$/, "")
          .replace(
            /^(?:(?:initial|session|entry|exit|current|prev_pageview)_)+/,
            "",
          );
        var safe;
        if (field === "attribution_referring_domain") {
          safe = safeDomain(value);
        } else if (field === "attribution_landing_path") {
          safe = attributionLandingPath(value);
        } else if (field === "url") {
          safe = publicUrl(value);
        } else if (field === "referrer" || field === "referring_domain") {
          safe =
            typeof value === "string"
              ? safeDomain(value) || referringDomain(value)
              : undefined;
        } else if (/^(?:pathname|path|host|hostname)$/.test(field)) {
          var prefix = key.slice(0, -field.length);
          var url =
            original[prefix + "current_url"] ?? original[prefix + "url"];
          var host = original[prefix + "host"] ?? original[prefix + "hostname"];
          var publicContext =
            (url === undefined || publicUrl(url)) &&
            (host === undefined ||
              host === "skyvern.com" ||
              host === "www.skyvern.com");
          if (!publicContext) {
            safe = undefined;
          } else if (field === "host" || field === "hostname") {
            safe =
              value === "skyvern.com" || value === "www.skyvern.com"
                ? value
                : undefined;
          } else {
            safe = docsPath(value);
          }
        } else if (PRIVATE_QUERY_FIELDS.has(field)) {
          safe = undefined;
        } else if (UTM_KEYS.includes(field)) {
          safe = safeUtm(field, value);
        } else {
          continue;
        }
        if (safe === undefined) delete properties[key];
        else properties[key] = safe;
      }
    }

    function getLivePostHog() {
      try {
        var posthog = livePostHog || window.posthog;
        if (
          posthog &&
          !Array.isArray(posthog) &&
          typeof posthog.set_config === "function"
        ) {
          return posthog;
        }
      } catch {}
    }

    function recapturePageview(posthog, event) {
      try {
        if (recapturingPageview) return;
        // Reuse page context only; the SDK must generate fresh identity fields.
        var properties = {
          $current_url: event.properties?.$current_url,
          $pathname: event.properties?.$pathname,
          $host: event.properties?.$host,
        };
        sanitizeProperties(properties);
        setTimeout(function () {
          try {
            if (hasAnalyticsConsent() !== false) return;
            recapturingPageview = true;
            posthog.capture("$pageview", properties);
          } catch {
          } finally {
            recapturingPageview = false;
          }
        }, 0);
      } catch {}
    }

    function parsePersistenceRecord(raw) {
      try {
        var record = JSON.parse(raw);
        if (!record || typeof record !== "object" || Array.isArray(record))
          return;
        for (var key of ["__proto__", "constructor", "prototype"])
          delete record[key];
        return record;
      } catch {}
    }

    function readSharedPersistence() {
      try {
        var name = "ph_" + KEY + "_posthog";
        var cookieRecord;
        var localRecord;
        try {
          var cookie = document.cookie
            .split(";")
            .map(function (value) {
              return value.trim();
            })
            .find(function (value) {
              return value.startsWith(name + "=");
            });
          if (cookie)
            cookieRecord = parsePersistenceRecord(
              decodeURIComponent(cookie.slice(name.length + 1)),
            );
        } catch {}
        try {
          localRecord = parsePersistenceRecord(localStorage.getItem(name));
        } catch {}
        // Match the SDK's default localStorage+cookie load precedence, without its write-on-read.
        var shared = { ...cookieRecord, ...localRecord };
        if (
          typeof shared.distinct_id !== "string" ||
          !shared.distinct_id ||
          shared.distinct_id === "$posthog_cookieless"
        )
          return;
        return trimStoredProperties(shared);
      } catch {}
    }

    function restorePersistentCapture(posthog, shared) {
      try {
        // SDK 1.434.10 migrates props in set_config; stage the shared snapshot before that write or capture.
        if (shared) posthog.persistence.props = shared;
        posthog.set_config({
          persistence: "localStorage+cookie",
          disable_persistence: false,
        });
        restorePersistence = false;
        return true;
      } catch {
        return false;
      }
    }

    function scheduleOptIn(posthog) {
      try {
        // Shared grants still need local persistence restored; never reset the shared identity.
        restorePersistence =
          restorePersistence ||
          posthog.config?.persistence === "memory" ||
          posthog.config?.disable_persistence === true;
        if (
          optInPending ||
          (posthog.has_opted_in_capturing() && !restorePersistence)
        ) {
          return;
        }
        optInPending = true;
        setTimeout(function () {
          try {
            if (hasAnalyticsConsent() !== true) return;
            if (restorePersistence) {
              var shared = posthog.has_opted_in_capturing()
                ? readSharedPersistence()
                : undefined;
              if (!restorePersistentCapture(posthog, shared)) return;
            }
            if (!posthog.has_opted_in_capturing()) {
              posthog.opt_in_capturing({ captureEventName: false });
            }
          } catch {
          } finally {
            optInPending = false;
          }
        }, 0);
      } catch {
        optInPending = false;
      }
    }

    function beforeSend(event) {
      try {
        var currentConsent = hasAnalyticsConsent();
        if (
          !currentConsent &&
          (currentConsent === null ||
            event?.properties?.$cookieless_mode !== true ||
            event?.properties?.distinct_id !== "$posthog_cookieless")
        ) {
          var posthog = getLivePostHog();
          if (posthog) {
            withdrawAnalyticsConsent(posthog);
            if (currentConsent === false && event?.event === "$pageview") {
              recapturePageview(posthog, event);
            }
          }
          return null;
        }
        if (!event || event.event !== "$pageview") return null;
        var domain =
          safeDomain(document.referrer) || referringDomain(document.referrer);
        event.properties = event.properties || {};
        if (domain) {
          event.properties.$referrer = domain;
          event.properties.$referring_domain = domain;
        } else {
          delete event.properties.$referrer;
          delete event.properties.$referring_domain;
        }
        if (event.properties?.distinct_id === "$posthog_cookieless") {
          event.properties.$cookieless_mode = true;
          if (event.properties.$cookieless_mode !== true) return null;
        }
        if (currentConsent === true) {
          var posthog = getLivePostHog();
          if (posthog) {
            scrubSessionEntryProps(posthog);
            scheduleOptIn(posthog);
          }
        }
        var campaign = parsePageUtm(window.location.search);
        if (Object.keys(campaign).length) {
          event.properties = { ...campaign, ...event.properties };
        }
        for (var properties of [
          event.properties,
          event.$set,
          event.$set_once,
        ]) {
          if (properties) sanitizeProperties(properties);
        }
        return event;
      } catch {
        // Drop the event if sanitizing fails, rather than sending its original properties.
        return null;
      }
    }

    var consent = hasAnalyticsConsent();
    var livePostHog;
    var recapturingPageview = false;
    var optInPending = false;
    var restorePersistence = false;
    if (!consent) purgeStaleConsent();

    // The site's snippet, defined directly because Mintlify already loads this file as a script.
    (function (document, posthog) {
      if (posthog.__SV) return;
      window.posthog = posthog;
      posthog._i = [];
      posthog.init = function (key, config, name) {
        try {
          function stubMethod(target, method) {
            var parts = method.split(".");
            if (parts.length === 2) {
              target = target[parts[0]];
              method = parts[1];
            }
            target[method] = function () {
              try {
                target.push(
                  [method].concat(Array.prototype.slice.call(arguments, 0)),
                );
              } catch {}
            };
          }
          var script = document.createElement("script");
          script.type = "text/javascript";
          script.async = true;
          script.src = config.api_host + "/static/array.js";
          var firstScript = document.getElementsByTagName("script")[0];
          firstScript.parentNode.insertBefore(script, firstScript);
          var instance = posthog;
          if (name !== undefined) instance = posthog[name] = [];
          else name = "posthog";
          instance.people = instance.people || [];
          instance.toString = function (withoutStub) {
            return (
              "posthog" +
              (name !== "posthog" ? "." + name : "") +
              (withoutStub ? "" : " (stub)")
            );
          };
          instance.people.toString = function () {
            return instance.toString(true) + ".people (stub)";
          };
          var methods = (
            "capture identify alias people.set people.set_once set_config register register_once " +
            "unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled " +
            "onFeatureFlags getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group " +
            "updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures getActiveMatchingSurveys getSurveys " +
            "onSessionId"
          ).split(" ");
          for (var i = 0; i < methods.length; i++)
            stubMethod(instance, methods[i]);
          posthog._i.push([key, config, name]);
        } catch {}
      };
      posthog.__SV = 1;
    })(document, existing || []);

    window.posthog.init(KEY, {
      api_host: "https://app.posthog.com",
      cookieless_mode: "on_reject",
      opt_out_capturing_by_default: true,
      cross_subdomain_cookie: true,
      persistence: "localStorage+cookie",
      save_campaign_params: false,
      // posthog-core.ts:1767-1773 copies raw URLs into the shared cookie before before_send.
      save_referrer: false,
      disable_capture_url_hashes: true,
      custom_campaign_params: [],
      cookie_persisted_properties: UTM_KEYS.concat([
        "attribution_referring_domain",
        "attribution_landing_path",
      ]),
      capture_pageview: "history_change",
      capture_pageleave: false,
      autocapture: false,
      capture_performance: false,
      disable_surveys: true,
      capture_heatmaps: false,
      capture_dead_clicks: false,
      capture_exceptions: false,
      disable_session_recording: true,
      advanced_disable_flags: true,
      mask_all_element_attributes: true,
      mask_all_text: true,
      mask_personal_data_properties: true,
      loaded: function (posthog) {
        try {
          livePostHog = posthog;
          guardPersistenceWrites(posthog);
          scrubSessionEntryProps(posthog);
        } catch {}
      },
      before_send: beforeSend,
    });
    if (consent) {
      try {
        window.posthog.opt_in_capturing({ captureEventName: false });
      } catch {}
    }
  } catch {}
})();
