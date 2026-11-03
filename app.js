(function () {
  var COLS = 5;
  var GAP = 36;
  var HOLD_MS = 8000;
  var FADE_MS = 1200;
  var POLL_MS = 180000;
  var wall = document.getElementById("wall");
  var statusEl = document.getElementById("status");
  var pins = [];
  var deck = [];
  var fresh = [];
  var known = {};
  var front = null;
  var back = null;
  var timer = 0;
  var polling = false;

  function showStatus(on) {
    if (on) statusEl.className = "";
    else statusEl.className = "hidden";
  }

  function shuffle(list) {
    var copy = list.slice();
    for (var i = copy.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i];
      copy[i] = copy[j];
      copy[j] = tmp;
    }
    return copy;
  }

  function sizeUrls(src) {
    var urls = [src];
    var sizes = ["originals", "736x", "564x", "1200x"];
    var i;
    for (i = 0; i < sizes.length; i++) {
      var next = src.replace(/i\.pinimg\.com\/[^/]+\//, "i.pinimg.com/" + sizes[i] + "/");
      if (urls.indexOf(next) === -1) urls.push(next);
    }
    return urls;
  }

  function loadImage(pin, done) {
    var urls = sizeUrls(pin.src);
    var img = new Image();
    var index = 0;
    function next() {
      if (index >= urls.length) {
        done(null);
        return;
      }
      img.onload = function () { done(img); };
      img.onerror = function () {
        index += 1;
        next();
      };
      img.src = urls[index];
    }
    next();
  }

  function takePins(count) {
    var chosen = [];
    var used = {};
    function consider(pin) {
      if (!pin || used[pin.id] || chosen.length >= count) return;
      used[pin.id] = true;
      chosen.push(pin);
    }
    while (fresh.length && chosen.length < count) consider(fresh.shift());
    var guard = 0;
    while (chosen.length < count && guard < pins.length * 2) {
      guard += 1;
      if (!deck.length) deck = shuffle(pins);
      consider(deck.shift());
    }
    return chosen;
  }

  function layout(list, viewW, viewH) {
    var colW = (viewW - GAP * (COLS + 1)) / COLS;
    var heights = [];
    var c;
    for (c = 0; c < COLS; c++) heights.push(GAP);
    var placed = [];
    var i;
    for (i = 0; i < list.length; i++) {
      var shortest = 0;
      for (c = 1; c < COLS; c++) {
        if (heights[c] < heights[shortest]) shortest = c;
      }
      var full = true;
      for (c = 0; c < COLS; c++) {
        if (heights[c] <= viewH) full = false;
      }
      if (full && placed.length >= COLS) break;
      var pin = list[i];
      var h = colW * (pin.height / pin.width);
      placed.push({
        pin: pin,
        x: GAP + shortest * (colW + GAP),
        y: heights[shortest],
        w: colW,
        h: h
      });
      heights[shortest] += h + GAP;
    }
    var wallH = heights[0];
    for (c = 1; c < COLS; c++) if (heights[c] < wallH) wallH = heights[c];
    return { placed: placed, wallW: viewW, wallH: Math.max(wallH, 1) };
  }

  function makeScene(placed, metrics, viewW, viewH) {
    var scene = document.createElement("div");
    scene.className = "scene";
    var board = document.createElement("div");
    board.className = "board";
    var scale = Math.max(viewW / metrics.wallW, viewH / metrics.wallH);
    board.style.width = metrics.wallW + "px";
    board.style.height = metrics.wallH + "px";
    board.style.transform = "translate(" + ((viewW - metrics.wallW * scale) / 2) + "px," + ((viewH - metrics.wallH * scale) / 2) + "px) scale(" + scale + ")";
    var origins = ["center center", "left top", "right bottom", "left bottom", "right top"];
    var i;
    for (i = 0; i < placed.length; i++) {
      var item = placed[i];
      var cell = document.createElement("div");
      cell.className = "cell";
      cell.style.left = item.x + "px";
      cell.style.top = item.y + "px";
      cell.style.width = item.w + "px";
      cell.style.height = item.h + "px";
      cell.style.background = item.pin.color || "#eeeeee";
      var img = item.image;
      img.style.transformOrigin = origins[i % origins.length];
      img.alt = "";
      cell.appendChild(img);
      board.appendChild(cell);
    }
    scene.appendChild(board);
    return scene;
  }

  function viewSize() {
    return { w: window.innerWidth || document.documentElement.clientWidth, h: window.innerHeight || document.documentElement.clientHeight };
  }

  function buildScene(done) {
    var view = viewSize();
    var estimate = Math.max(COLS * 3, 12);
    var batch = takePins(estimate + COLS);
    var plan = layout(batch, view.w, view.h);
    var pending = plan.placed.length;
    if (!pending) {
      done(null);
      return;
    }
    var loaded = 0;
    var failed = 0;
    plan.placed.forEach(function (item) {
      loadImage(item.pin, function (img) {
        item.image = img;
        if (!img) failed += 1;
        loaded += 1;
        if (loaded === pending) {
          var ok = [];
          var i;
          for (i = 0; i < plan.placed.length; i++) {
            if (plan.placed[i].image) ok.push(plan.placed[i]);
          }
          if (!ok.length) {
            done(null);
            return;
          }
          var metrics = layout(ok.map(function (item) { return item.pin; }), view.w, view.h);
          var byId = {};
          for (i = 0; i < ok.length; i++) byId[ok[i].pin.id] = ok[i].image;
          var placed = metrics.placed;
          for (i = 0; i < placed.length; i++) placed[i].image = byId[placed[i].pin.id];
          done(makeScene(placed, metrics, view.w, view.h));
        }
      });
    });
  }

  function showScene(scene) {
    wall.appendChild(scene);
    var _unused = scene.offsetWidth;
    scene.className = "scene on";
    if (front) {
      var old = front;
      setTimeout(function () {
        if (old.parentNode) old.parentNode.removeChild(old);
      }, FADE_MS + 40);
    }
    front = scene;
  }

  function advance() {
    buildScene(function (scene) {
      if (!scene) {
        timer = setTimeout(advance, 4000);
        return;
      }
      showScene(scene);
      showStatus(false);
      timer = setTimeout(advance, HOLD_MS);
    });
  }

  function applyPins(next) {
    var i;
    var arrived = [];
    var nextKnown = {};
    for (i = 0; i < next.length; i++) {
      var pin = next[i];
      if (!pin || !pin.id || !pin.src || !pin.width || !pin.height) continue;
      nextKnown[pin.id] = true;
      if (!known[pin.id]) arrived.push(pin);
    }
    if (!pins.length) {
      pins = next;
      deck = shuffle(next);
      known = nextKnown;
      showStatus(false);
      advance();
      return;
    }
    pins = next;
    known = nextKnown;
    for (i = arrived.length - 1; i >= 0; i--) fresh.unshift(arrived[i]);
  }

  function fetchPins() {
    if (polling) return;
    polling = true;
    var url = "pins.json?t=" + Date.now();
    fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("bad status");
      return response.json();
    }).then(function (data) {
      polling = false;
      var list = data && data.pins ? data.pins : [];
      if (!list.length) {
        if (!pins.length) showStatus(true);
        return;
      }
      applyPins(list);
    }, function () {
      polling = false;
      if (!pins.length) showStatus(true);
    });
  }

  function requestWake() {
    if (!navigator.wakeLock || !navigator.wakeLock.request) return;
    navigator.wakeLock.request("screen").then(function () {}, function () {});
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) requestWake();
  });
  requestWake();
  fetchPins();
  setInterval(fetchPins, POLL_MS);
  setInterval(function () {
    if (!pins.length) fetchPins();
  }, 20000);
})();
