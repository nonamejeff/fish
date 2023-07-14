window.addEventListener('DOMContentLoaded', function() {
  const innerBox = document.querySelector('.inner-box');
  const gradientOverlay = document.createElement('div');
  gradientOverlay.classList.add('gradient-overlay');

  innerBox.appendChild(gradientOverlay);

  window.addEventListener('scroll', function() {
    const scrolled = window.pageYOffset || document.documentElement.scrollTop;
    gradientOverlay.style.backgroundPositionY = -scrolled + 'px';
  });
});