const DESKTOP = "(min-width: 768px)";

class Viewport {
  desktop = $state(true);

  constructor() {
    if (typeof matchMedia !== "function") return;
    const query = matchMedia(DESKTOP);
    this.desktop = query.matches;
    query.addEventListener("change", (event) => (this.desktop = event.matches));
  }
}

export const viewport = new Viewport();
