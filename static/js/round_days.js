// Day picker on the round tab bars (_day_picker.html): when a day is chosen,
// open a round on it so the content below matches the tabs on show. The
// round in play — the first not finished — or else the day's last round,
// which for standings is "after day N". Stays put if the open round is
// already on that day.
window.showRoundDay = function (day) {
    if (day === "0") return;
    const tabs = [...document.querySelectorAll(`.round-tab[data-day="${day}"]`)];
    if (!tabs.length || tabs.some((t) => t.classList.contains("active"))) return;
    const pick = tabs.find((t) => !t.classList.contains("round-tab-finished")) ?? tabs.at(-1);
    pick.click();
};
