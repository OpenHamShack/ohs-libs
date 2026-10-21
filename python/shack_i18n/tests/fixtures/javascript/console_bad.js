// el.textContent = "Inside a comment" is not a finding.
/* Nor is alert("this"). */
const re = /"not a string"/g;  // a regex literal with quotes inside
function render(el, n, name) {
  el.textContent = "Transmit blocked";  // expect-finding
  el.innerText = `Heard ${name}`;  // expect-finding
  el.innerHTML = '<span class="dot"></span> Ready';  // expect-finding
  el.innerHTML = "<b>" + name + "</b> is on air";  // expect-finding
  el.title = 'Click to tune';  // expect-finding
  input.placeholder = "Callsign"  // expect-finding
  alert("Saved.");  // expect-finding
  if (confirm("Release the radio?")) {}  // expect-finding
  btn.setAttribute("aria-label", "Push to talk");  // expect-finding
  new Notification("Transmitting");  // expect-finding
  toast("Settings saved");  // expect-finding (configured sink)
  el.textContent = n > 1
    ? "Many stations"  // expect-finding
    : "One station";  // expect-finding
  const half = total / 2; el.title = "Halfway"  // expect-finding (a division, then a sink)
}
