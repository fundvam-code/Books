document.getElementById("hide-printed").addEventListener("change", (event) => {
    event.target.form.requestSubmit();
});

setTimeout(() => refreshLabelPage(), 5 * 60 * 1000);
