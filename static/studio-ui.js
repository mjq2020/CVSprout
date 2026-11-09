// Native disclosures keep filters and infrequent project actions out of the
// working surface. One delegated listener survives workspace rerenders.
let installed = false;

export function installStudioInteractions() {
  if (installed) return;
  installed = true;
  const disclosures = () => document.querySelectorAll('[data-studio-disclosure][open]');
  const close = (details, restore = false) => {
    details.open = false;
    if (restore) details.querySelector('summary')?.focus({ preventScroll: true });
  };
  document.addEventListener('click', event => {
    for (const details of disclosures()) {
      if (!details.contains(event.target)) close(details, details.contains(document.activeElement));
      // Return modal focus to the visible trigger, not a hidden menu action.
      else if (details.matches('.project-options') && event.target.closest('button')) close(details, true);
    }
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || document.querySelector('#modal-root .modal')) return;
    const details = [...disclosures()].at(-1);
    if (!details) return;
    event.preventDefault();
    event.stopPropagation();
    close(details, true);
  }, true);
  document.addEventListener('focusin', event => {
    for (const details of disclosures()) if (!details.contains(event.target)) close(details);
  });
}
