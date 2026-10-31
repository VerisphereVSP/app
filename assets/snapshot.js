/* patch_share_interactive — the share page's only script, served same-origin at /c/snapshot.js (CSP: script-src 'self').
   Support / Challenge records an anonymous opinion (daily counts, nothing about the visitor), then the page explains the
   game in three lines and offers one next step: Back it with VSP. The visitor's own pick is remembered in localStorage. */
(function () {
  "use strict";
  var sec = document.getElementById("take-side");
  if (!sec) return;
  var pid = sec.getAttribute("data-post");
  var pctAttr = sec.getAttribute("data-pct");           // % of stake supporting, or "" when nothing is staked
  var pct = pctAttr === "" ? null : parseInt(pctAttr, 10);
  var app = sec.getAttribute("data-app");
  var key = "vsp_opinion_" + pid;
  var buttons = sec.querySelectorAll("button[data-side]");
  var verdict = document.getElementById("ts-verdict");
  var readers = document.getElementById("ts-readers");
  var how = document.getElementById("ts-how");
  var back = document.getElementById("ts-back");
  var ask = document.getElementById("ts-ask");
  var switchWrap = document.getElementById("ts-switch-wrap");
  var switchLink = document.getElementById("ts-switch");
  var current = null;  // patch_share_switch: the side currently counted for this visitor
  var invite = document.getElementById("ts-invite");
  var codeSuffix = "";  // patch_invites: "&code=…" when the link carries an open invitation
  var START = "&start=1";  // patch_share_start: the app opens its Start walk-through for every reader who taps the button

  function remembered() { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function remember(side) { try { localStorage.setItem(key, side); } catch (e) { /* private mode */ } }

  function line(side) {
    if (pct === null) return "No one has staked on this claim yet — you would be the first to take a side with VSP.";
    var mine = side === "support" ? pct : 100 - pct;
    if (mine >= 50) return "You and " + mine + " % of the stake agree.";
    return "You are with the " + mine + " % " + (side === "support" ? "backing it" : "challenging it") + "; " + (100 - mine) + " % of the stake is on the other side.";
  }

  function show(side, counts) {
    current = side;
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      b.disabled = true;
      b.className = "side " + (b.getAttribute("data-side") === side ? "chosen" : "other");
    }
    if (ask) ask.textContent = side === "support" ? "You support this claim." : "You challenge this claim.";
    verdict.textContent = line(side);
    verdict.hidden = false;
    if (counts && typeof counts.support_n === "number") {
      var total = counts.support_n + counts.challenge_n;
      readers.textContent = total === 1 ? "You are the first reader to take a side here."
        : "Readers here: " + counts.support_n + " support · " + counts.challenge_n + " challenge.";
      readers.hidden = false;
    }
    how.hidden = false;
    back.href = app + "&side=" + side + START + codeSuffix;
    back.hidden = false;
    if (switchWrap && switchLink) {
      switchLink.textContent = side === "support" ? "Challenge instead" : "Support instead";
      switchWrap.hidden = false;
    }
  }

  function request(method, body) {
    return fetch("/api/claims/" + pid + "/opinion", {
      method: method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }

  for (var i = 0; i < buttons.length; i++) {
    (function (b) {
      b.addEventListener("click", function () {
        var side = b.getAttribute("data-side");
        remember(side);
        show(side, null);
        request("POST", { side: side }).then(function (counts) { if (counts) show(side, counts); });
      });
    })(buttons[i]);
  }

  if (switchLink) {
    switchLink.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (!current) return;
      var from = current, side = current === "support" ? "challenge" : "support";
      remember(side);
      show(side, null);
      request("POST", { side: side, from_side: from }).then(function (counts) { if (counts) show(side, counts); });
    });
  }

  // patch_invites — an invitation in the link: say what is on the table, and hand the code to the app's Start flow
  var code = null;
  try { code = new URLSearchParams(window.location.search).get("code"); } catch (e) { /* old browser */ }
  if (code && invite) {
    fetch("/api/invites/peek?code=" + encodeURIComponent(code)).then(function (r) { return r.ok ? r.json() : null; }).then(function (p) {
      if (!p) { invite.textContent = "The invitation in this link is not valid."; invite.hidden = false; return; }
      if (p.status === "open") {
        codeSuffix = "&code=" + encodeURIComponent(code);
        invite.textContent = "Someone put " + p.amount_vsp + " VSP on the table for you. Pick a side, then claim it and back your side.";
        back.textContent = "Claim your " + p.amount_vsp + " VSP and back it";
        if (current) back.href = app + "&side=" + current + START + codeSuffix;
      } else {
        invite.textContent = "The invitation in this link was already " + p.status + ".";
      }
      invite.hidden = false;
    }).catch(function () { /* the page works without it */ });
  }

  var prev = remembered();
  if (prev === "support" || prev === "challenge") {
    show(prev, null);
    request("GET").then(function (counts) { if (counts) show(prev, counts); });
  }
})();
