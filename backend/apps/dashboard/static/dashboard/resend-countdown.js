// "Resend email" cooldown on the Check-your-email page. Progressive
// enhancement only: without JS the button simply works (the server-side
// "resend" rate limit is the real guard). External file because the CSP has
// no 'unsafe-inline'.
(() => {
  "use strict";

  const start = () => {
    const button = document.getElementById("resend-button");
    const status = document.getElementById("resend-countdown");
    if (!button) return;
    let remaining = parseInt(button.dataset.cooldown || "60", 10);
    if (!(remaining > 0)) return;

    const render = () => {
      if (!status) return;
      status.hidden = false;
      status.textContent = `You can resend in ${remaining}s.`;
    };

    button.disabled = true;
    render();
    const timer = window.setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        window.clearInterval(timer);
        button.disabled = false;
        if (status) {
          status.textContent = "";
          status.hidden = true;
        }
        return;
      }
      render();
    }, 1000);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
