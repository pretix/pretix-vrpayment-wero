$(function () {
    window.setTimeout(function () {
        let searchParams = new URLSearchParams(window.location.search);
        let click = (searchParams.has("click")) ? searchParams.get("click") : "1";
        location.href = window.location.pathname + "?click=" + click;
    }, 2000);
});
