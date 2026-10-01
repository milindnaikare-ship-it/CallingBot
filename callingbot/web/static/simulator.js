// Browser call simulator: plays the telephony provider's role against /api/simulator/*.
// Vanilla JS, no dependencies. All text is inserted with textContent (never innerHTML) because
// it comes from the LLM and from the person typing.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    distributor: $("sim-distributor"),
    language: $("sim-language"),
    start: $("sim-start"),
    speak: $("sim-speak"),
    transcript: $("sim-transcript"),
    placeholder: $("sim-placeholder"),
    form: $("sim-form"),
    text: $("sim-text"),
    send: $("sim-send"),
    silence: $("sim-silence"),
    hangup: $("sim-hangup"),
    status: $("sim-status"),
  };
  if (!els.start) { return; }

  var callId = null;
  var busy = false;

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var detail = data && data.detail;
          if (typeof detail !== "string") { detail = JSON.stringify(detail || res.statusText); }
          throw new Error(res.status + ": " + detail);
        }
        return data;
      });
    });
  }

  function bubble(role, text, note) {
    if (els.placeholder) { els.placeholder.remove(); els.placeholder = null; }
    var div = document.createElement("div");
    div.className = "bubble " + role;
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = (role === "bot" ? "Bot" : role === "distributor" ? "You" : "System") + (note ? " · " + note : "");
    div.appendChild(meta);
    div.appendChild(document.createTextNode(text));
    els.transcript.appendChild(div);
    els.transcript.scrollTop = els.transcript.scrollHeight;
  }

  function speak(lines, language) {
    if (!els.speak.checked || !("speechSynthesis" in window)) { return; }
    lines.forEach(function (line) {
      var u = new SpeechSynthesisUtterance(line);
      if (language) { u.lang = language; }
      window.speechSynthesis.speak(u);
    });
  }

  function showBot(data) {
    var say = data.say || [];
    say.forEach(function (line) { bubble("bot", line); });
    speak(say, data.language);
    if (data.action === "transfer") {
      bubble("system", "The bot transfers the call to " + (data.transfer_to || "a relationship manager") + ".");
    }
  }

  function setActive(active) {
    els.text.disabled = !active;
    els.send.disabled = !active;
    els.silence.disabled = !active;
    els.hangup.disabled = !active;
    els.start.disabled = active;
    els.distributor.disabled = active;
    els.language.disabled = active;
    if (active) { els.text.focus(); }
  }

  function showStatus(result) {
    els.status.hidden = false;
    els.status.replaceChildren();
    var p = document.createElement("p");
    p.textContent = "Call ended. Status: " + (result.status || "-") + ". Outcome: " + (result.outcome || "none") + ".";
    els.status.appendChild(p);
    var messages = result.messages || [];
    var title = document.createElement("p");
    title.textContent = messages.length ? "Messages sent:" : "No messages were sent.";
    els.status.appendChild(title);
    if (messages.length) {
      var ul = document.createElement("ul");
      messages.forEach(function (m) {
        var li = document.createElement("li");
        li.textContent = m.channel + " to " + m.destination + " [" + m.status + "]" + (m.link ? " - " + m.link : "");
        ul.appendChild(li);
      });
      els.status.appendChild(ul);
    }
    var link = document.createElement("a");
    link.href = "/calls/" + encodeURIComponent(callId);
    link.textContent = "Open call #" + callId + " (transcript, audit trail)";
    els.status.appendChild(link);
  }

  function fail(err) {
    bubble("system", "Error: " + err.message);
    busy = false;
  }

  function finish(result) {
    setActive(false);
    showStatus(result);
    busy = false;
  }

  els.start.addEventListener("click", function () {
    if (busy) { return; }
    busy = true;
    els.transcript.replaceChildren();
    els.placeholder = null;
    els.status.hidden = true;
    var distributorId = els.distributor.value ? parseInt(els.distributor.value, 10) : null;
    post("/api/simulator/calls", { distributor_id: distributorId, language: els.language.value || null })
      .then(function (data) {
        callId = data.call_id;
        bubble("system", "Call #" + callId + " connected.");
        showBot(data);
        busy = false;
        if (data.action === "gather") {
          setActive(true);
        } else {
          return post("/api/simulator/calls/" + callId + "/hangup").then(finish);
        }
      })
      .catch(fail);
  });

  function sendInput(text) {
    if (busy || callId === null) { return; }
    busy = true;
    bubble("distributor", text === null ? "(silence)" : text);
    post("/api/simulator/calls/" + callId + "/input", { text: text })
      .then(function (data) {
        showBot(data);
        if (data.ended) { finish(data); } else { busy = false; els.text.focus(); }
      })
      .catch(fail);
  }

  els.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = els.text.value.trim();
    if (!text) { return; }
    els.text.value = "";
    sendInput(text);
  });

  els.silence.addEventListener("click", function () { sendInput(null); });

  els.hangup.addEventListener("click", function () {
    if (busy || callId === null) { return; }
    busy = true;
    bubble("system", "You hung up.");
    post("/api/simulator/calls/" + callId + "/hangup").then(finish).catch(fail);
  });
})();
