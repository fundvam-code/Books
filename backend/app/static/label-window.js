const printImmediately = document.getElementById("print-immediately");

document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-label-open]");
    if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const form = link.closest("[data-order-label]").querySelector(".label-output");
    event.preventDefault();
    form.requestSubmit();
});

for (const form of document.querySelectorAll(".label-output")) {
    form.addEventListener("submit", () => {
        form.elements.autoprint.disabled = !printImmediately.checked;
        const name = "ozon-label-" + crypto.randomUUID();
        const preview = window.open("", name, "popup,width=940,height=780");
        if (preview) {
            form.target = name;
            form.removeAttribute("rel");
            setTimeout(() => { preview.opener = null; }, 0);
        } else {
            form.target = "_blank";
            form.rel = "noopener";
        }
    });
}
