import { t, fmt } from "./shack-i18n.js";

function render(el, n, name) {
  el.textContent = t("tx.blocked");
  el.innerText = t("heard", { name });
  el.innerHTML = `<span class="dot"></span> ${t("ready")}`;
  el.innerHTML = "<b>" + name + "</b>";
  el.title = name;
  el.textContent = fmt.frequency(14074000, "en");
  btn.setAttribute("data-mode", "usb-wide mode");  // not a text attribute
  btn.setAttribute("aria-label", t("ptt"));
  console.log("Transmit pressed");  // log, not a sink
  el.className = "big red button";
  const url = "/api/v1/status";
  el.textContent = "—";
  el.textContent = "ft8";  // an identifier
}
