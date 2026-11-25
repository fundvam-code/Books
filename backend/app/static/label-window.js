const printImmediately = document.getElementById("print-immediately");
const titleOptions = document.querySelectorAll('.label-output input[name="with_title"]');
const { autoRefreshTitles, ...historyState } = history.state ?? {};

if (autoRefreshTitles) {
    const checkedForms = new Set(autoRefreshTitles);
    for (const option of titleOptions) {
        option.checked = checkedForms.has(option.form.action);
    }
    history.replaceState(historyState, "");
}

let refreshTimer;
function refreshLabelPage() {
    clearTimeout(refreshTimer);
    if (document.querySelector(".label-load button:disabled")) {
        refreshTimer = setTimeout(refreshLabelPage, 2000);
        return;
    }

    const checkedForms = Array.from(titleOptions)
        .filter((option) => option.checked)
        .map((option) => option.form.action);
    history.replaceState({ ...history.state, autoRefreshTitles: checkedForms }, "");
    window.location.reload();
}

window.addEventListener("storage", (event) => {
    if (event.key === "fbs-print-submission" && event.newValue) refreshLabelPage();
});

if (printImmediately) {
    const preferenceKey = "ozon-label-print-immediately";
    const restorePreference = () => {
        try {
            printImmediately.checked = localStorage.getItem(preferenceKey) === "true";
        } catch {
            // The checkbox still works on the current page when storage is unavailable.
        }
    };

    restorePreference();
    window.addEventListener("pageshow", restorePreference);
    printImmediately.addEventListener("change", () => {
        try {
            localStorage.setItem(preferenceKey, String(printImmediately.checked));
        } catch {
            // Keep the user's current selection even when it cannot be saved.
        }
    });
}

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
