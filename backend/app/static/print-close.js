try {
    localStorage.setItem("fbs-print-submission", crypto.randomUUID());
} finally {
    window.close();
}
