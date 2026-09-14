(() => {
  const body = document.getElementById("terminal-body");
  const root = document.documentElement;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const exchanges = {
    "on-device": {
      accent: "#f0b429",
      question: "does this need wifi?",
      answer: "no. on-device runs fully offline, right on your mac.",
    },
    "cloud-pro": {
      accent: "#38d9c9",
      question: "does this need wifi?",
      answer: "yes. cloud pro reasons on apple's servers for harder questions.",
    },
  };

  let renderToken = 0;

  function render(model, { animate }) {
    const { question, answer, accent } = exchanges[model];
    root.style.setProperty("--accent", accent);

    const you = document.createElement("div");
    you.className = "you";
    you.textContent = `you › ${question}`;

    const fm = document.createElement("div");
    fm.className = "fm";

    body.replaceChildren(you, fm);

    const cursor = document.createElement("span");
    cursor.className = "cursor";

    if (!animate || reduceMotion) {
      fm.textContent = `fm-pcc › ${answer}`;
      fm.appendChild(cursor);
      return;
    }

    const token = ++renderToken;
    const full = `fm-pcc › ${answer}`;
    let i = 0;

    (function type() {
      if (token !== renderToken) return;
      fm.textContent = full.slice(0, i);
      fm.appendChild(cursor);
      if (i < full.length) {
        i += 1;
        setTimeout(type, 14);
      }
    })();
  }

  document.querySelectorAll(".toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("active")) return;
      document.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      render(btn.dataset.model, { animate: true });
    });
  });

  render("on-device", { animate: true });
})();
