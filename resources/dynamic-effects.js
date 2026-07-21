(function() {
  'use strict';

  // Only run on desktop
  if (window.innerWidth < 800) {
    return;
  }

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }

  if (!window.DYNAMIC_EFFECT || window.DYNAMIC_EFFECT === 'none') {
    return;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function getPerformanceProfile() {
    var ratio = 1;
    var cores = navigator.hardwareConcurrency;
    var memory = navigator.deviceMemory;
    var dpr = window.devicePixelRatio || 1;
    var viewportArea = (window.innerWidth * window.innerHeight) / 1000000;

    if (cores && cores <= 2) {
      ratio *= 0.55;
    } else if (cores && cores <= 4) {
      ratio *= 0.75;
    }

    if (memory && memory <= 2) {
      ratio *= 0.65;
    } else if (memory && memory <= 4) {
      ratio *= 0.8;
    }

    if (dpr >= 2.5) {
      ratio *= 0.75;
    } else if (dpr >= 2) {
      ratio *= 0.85;
    }

    if (viewportArea >= 2.5) {
      ratio *= 0.8;
    } else if (viewportArea >= 1.5) {
      ratio *= 0.9;
    }

    return {
      particleRatio: clamp(ratio, 0.45, 1),
      fpsLimit: ratio < 0.65 ? 24 : 30
    };
  }

  var performanceProfile = getPerformanceProfile();

  function scaleParticleCount(value, minimum) {
    return Math.max(minimum, Math.round(value * performanceProfile.particleRatio));
  }

  function mergeParticleOptions(options) {
    options.fpsLimit = performanceProfile.fpsLimit;
    options.detectRetina = false;
    options.pauseOnBlur = true;
    options.pauseOnOutsideViewport = true;
    return options;
  }

  // Create container for effects
  function createContainer() {
    var container = document.getElementById('dynamic-effects-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'dynamic-effects-container';
      document.body.appendChild(container);
    }
    container.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 9999;';
    return container;
  }

  // Load tsParticles for effects
  function loadTsParticles(callback) {
    if (typeof tsParticles !== 'undefined') {
      callback();
      return;
    }

    var script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/tsparticles@2/tsparticles.bundle.min.js';
    script.onload = callback;
    document.head.appendChild(script);
  }

  function loadParticleEffect(containerId, options) {
    return tsParticles.load(containerId, mergeParticleOptions(options));
  }

  // Snowflakes effect using tsParticles (blue snowflakes, gentle floating like blossoms)
  function initSnowflakes() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(25, 12), density: { enable: true, area: 800 } },
            color: { value: ['#87CEEB', '#B0E0E6', '#ADD8E6', '#E0FFFF'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  value: ['❄', '❅', '❆', '✻'],
                  font: 'Segoe UI Emoji',
                  weight: 400
                }
              }
            },
            opacity: { value: { min: 0.4, max: 0.9 } },
            size: { value: { min: 8, max: 14 } },
            move: {
              enable: true,
              speed: 0.5,
              direction: 'bottom',
              straight: true,
              outModes: { default: 'out' }
            },
            rotate: {
              value: { min: 0, max: 360 },
              direction: 'random',
              animation: { enable: true, speed: 2 }
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  // Snow effect using tsParticles (small falling snow particles, slower)
  function initSnow() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(60, 28), density: { enable: true, area: 800 } },
            color: { value: '#ffffff' },
            shape: { type: 'circle' },
            opacity: { value: { min: 0.3, max: 0.8 } },
            size: { value: { min: 1, max: 4 } },
            move: {
              enable: true,
              speed: 0.8,
              direction: 'bottom',
              straight: false,
              outModes: { default: 'out' },
              random: true
            },
            wobble: {
              enable: true,
              distance: 8,
              speed: 3
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  // Cherry blossoms effect using tsParticles (slow falling petals)
  function initCherryBlossoms() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(20, 10), density: { enable: true, area: 800 } },
            color: { value: ['#ffb7c5', '#ffc0cb', '#ff69b4', '#ffaec9'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  value: ['❀', '✿', '❁', '✾'],
                  font: 'Segoe UI Emoji',
                  weight: 400
                }
              }
            },
            opacity: { value: { min: 0.5, max: 0.9 } },
            size: { value: { min: 8, max: 14 } },
            move: {
              enable: true,
              speed: 0.4,
              direction: 'bottom',
              straight: true,
              outModes: { default: 'out' }
            },
            rotate: {
              value: { min: 0, max: 360 },
              direction: 'random',
              animation: { enable: true, speed: 2 }
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  // Red poinciana / phoenix flower (Hoa Phượng) effect.
  // Keep the same gentle pace as cherry blossoms, but use a warmer red-orange palette
  // and a custom five-petal flower with a pale spotted banner petal and long stamens.
  function initRedPoinciana() {
    var container = createContainer();
    container.className = (container.className + ' poinciana-effects').trim();

    if (!document.getElementById('poinciana-effects-style')) {
      var style = document.createElement('style');
      style.id = 'poinciana-effects-style';
      style.textContent = [
        '.poinciana-effects .poinciana-flower,.poinciana-effects .poinciana-petal{',
        'position:absolute;top:-8vh;left:0;will-change:transform,opacity;',
        'filter:drop-shadow(0 0 3px rgba(255,58,31,.45));',
        'animation-name:poinciana-fall;animation-timing-function:linear;animation-iteration-count:infinite;',
        '}',
        '.poinciana-effects .poinciana-petal{filter:none;}',
        '.poinciana-effects svg{display:block;width:100%;height:100%;}',
        '@keyframes poinciana-fall{',
        '0%{transform:translate3d(var(--x-start),-12vh,0) rotate(var(--r-start));opacity:0;}',
        '8%{opacity:var(--opacity);}',
        '100%{transform:translate3d(var(--x-end),112vh,0) rotate(var(--r-end));opacity:0;}',
        '}'
      ].join('');
      document.head.appendChild(style);
    }

    var flowerSvg = [
      '<svg viewBox="-38 -42 76 84" aria-hidden="true">',
      '<g>',
      '<path d="M0 -8 C-11 -35 19 -42 17 -14 C35 -23 44 4 18 7 C31 30 5 39 -5 14 C-24 31 -42 8 -17 0 C-33 -19 -12 -35 0 -8Z" fill="#e31b23"/>',
      '<path d="M0 -10 C-8 -32 16 -38 14 -13 C31 -21 39 3 16 5 C27 25 6 33 -4 12 C-21 27 -35 8 -14 0 C-28 -16 -10 -30 0 -10Z" fill="#ff3b1f" opacity=".86"/>',
      '<path d="M-4 -12 C-10 -38 15 -44 22 -18 C19 -8 8 -2 0 -5Z" fill="#fff2a6"/>',
      '<path d="M-2 -13 C4 -20 11 -21 18 -17 M1 -9 C7 -12 12 -12 17 -9 M2 -5 C7 -6 11 -5 15 -2" fill="none" stroke="#c8102e" stroke-width="2" stroke-linecap="round" opacity=".8"/>',
      '<circle cx="4" cy="-15" r="1.5" fill="#c8102e"/><circle cx="10" cy="-13" r="1.2" fill="#c8102e"/><circle cx="14" cy="-7" r="1.2" fill="#c8102e"/>',
      '<path d="M0 5 C-3 15 -10 22 -18 30 M3 5 C3 17 -1 25 -6 35 M6 4 C10 15 15 22 22 31" fill="none" stroke="#b40016" stroke-width="2" stroke-linecap="round"/>',
      '<circle cx="-18" cy="30" r="2" fill="#ffd166"/><circle cx="-6" cy="35" r="2" fill="#ffd166"/><circle cx="22" cy="31" r="2" fill="#ffd166"/>',
      '<circle cx="0" cy="2" r="5" fill="#9c0012"/>',
      '</g>',
      '</svg>'
    ].join('');

    var petalSvg = [
      '<svg viewBox="-14 -12 28 24" aria-hidden="true">',
      '<path d="M0 -10 C10 -6 12 5 0 11 C-12 5 -10 -6 0 -10Z" fill="#e31b23"/>',
      '<path d="M0 -8 C5 -4 6 4 0 8" fill="none" stroke="#ff7a18" stroke-width="1.4" stroke-linecap="round" opacity=".75"/>',
      '</svg>'
    ].join('');

    function addFallingItem(className, svg, index, total, minSize, maxSize, minDuration, maxDuration) {
      var item = document.createElement('div');
      var size = minSize + Math.random() * (maxSize - minSize);
      var xStart = Math.random() * 100;
      var drift = -8 + Math.random() * 16;
      var duration = minDuration + Math.random() * (maxDuration - minDuration);
      var delay = -(index / total) * duration;
      var opacity = 0.55 + Math.random() * 0.35;

      item.className = className;
      item.style.width = size + 'px';
      item.style.height = size + 'px';
      item.style.setProperty('--x-start', xStart + 'vw');
      item.style.setProperty('--x-end', Math.max(-5, Math.min(105, xStart + drift)) + 'vw');
      item.style.setProperty('--r-start', Math.floor(Math.random() * 360) + 'deg');
      item.style.setProperty('--r-end', Math.floor(360 + Math.random() * 540) + 'deg');
      item.style.setProperty('--opacity', opacity.toFixed(2));
      item.style.animationDuration = duration + 's';
      item.style.animationDelay = delay + 's';
      item.innerHTML = svg;
      container.appendChild(item);
    }

    var flowerCount = scaleParticleCount(20, 10);
    var petalCount = scaleParticleCount(14, 7);

    for (var i = 0; i < flowerCount; i++) {
      addFallingItem('poinciana-flower', flowerSvg, i, flowerCount, 18, 30, 24, 34);
    }
    for (var j = 0; j < petalCount; j++) {
      addFallingItem('poinciana-petal', petalSvg, j, petalCount, 7, 13, 24, 34);
    }
  }

  // Rain effect (falling raindrops - thin lines)
  function initRain() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(150, 65), density: { enable: true, area: 800 } },
            color: { value: ['#87CEEB', '#B0E0E6', '#ADD8E6', '#6eb5ff'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  value: ['|'],
                  font: 'monospace',
                  weight: 400
                }
              }
            },
            opacity: { value: { min: 0.3, max: 0.6 } },
            size: { value: { min: 6, max: 12 } },
            move: {
              enable: true,
              speed: 6,
              direction: 'bottom',
              straight: true,
              outModes: { default: 'out' }
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  // Fireflies effect (slow floating glowing particles - yellow/orange only)
  function initFireflies() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(40, 18), density: { enable: true, area: 800 } },
            color: { value: ['#ffff66', '#ffd700', '#ffa500', '#ffb347', '#ffe135'] },
            shape: { type: 'circle' },
            opacity: {
              value: { min: 0.1, max: 1 },
              animation: { enable: true, speed: 1.5, minimumValue: 0.1, sync: false }
            },
            size: { value: { min: 2, max: 4 } },
            move: {
              enable: true,
              speed: 0.5,
              direction: 'none',
              outModes: { default: 'bounce' },
              random: true
            },
            shadow: {
              enable: true,
              color: '#ffa500',
              blur: Math.max(8, Math.round(15 * performanceProfile.particleRatio))
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  // Lunar New Year (Tết) effect - combined festive elements
  // Includes: red envelopes, yellow blossoms (Hoa Mai), lanterns, sparkles, gold coins
  function initLunarNewYear() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        // Create multiple particle systems for different behaviors
        // Main falling particles (envelopes, blossoms, coins)
        loadParticleEffect('dynamic-effects-container', {
          particles: {
            number: { value: scaleParticleCount(30, 14), density: { enable: true, area: 800 } },
            color: { value: ['#ff0000', '#ffd700', '#ffcc00', '#ff4500'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  // Vietnamese Tết: red envelopes 🧧, Hoa Mai blossoms ❀✿, firecrackers 🧨, fan 🪭
                  value: ['🧧', '❀', '✿', '🧨', '🪭', '❀', '✿', '🧧', '❀', '🧧'],
                  font: 'Segoe UI Emoji',
                  weight: 400
                }
              }
            },
            opacity: { value: { min: 0.6, max: 1 } },
            size: { value: { min: 8, max: 14 } },
            move: {
              enable: true,
              speed: 0.25,
              direction: 'bottom',
              straight: false,
              outModes: { default: 'out' }
            },
            rotate: {
              value: { min: 0, max: 360 },
              direction: 'random',
              animation: { enable: true, speed: 1.5 }
            },
            wobble: {
              enable: true,
              distance: 8,
              speed: 3
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });

        // Create second container for rising lanterns
        var lanternContainer = document.getElementById('lantern-effects-container');
        if (!lanternContainer) {
          lanternContainer = document.createElement('div');
          lanternContainer.id = 'lantern-effects-container';
          lanternContainer.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 9998;';
          document.body.appendChild(lanternContainer);
        }
        loadParticleEffect('lantern-effects-container', {
          particles: {
            number: { value: scaleParticleCount(5, 3), density: { enable: true, area: 1000 } },
            color: { value: ['#ff4500', '#ff6600', '#ff0000'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  value: ['🏮'],
                  font: 'Segoe UI Emoji',
                  weight: 400
                }
              }
            },
            opacity: { value: { min: 0.7, max: 1 } },
            size: { value: { min: 18, max: 26 } },
            move: {
              enable: true,
              speed: 0.2,
              direction: 'top',
              straight: false,
              outModes: { default: 'out' },
              random: true
            },
            rotate: {
              value: { min: -5, max: 5 },
              animation: { enable: true, speed: 0.3 }
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });

        // Create third container for twinkling sparkles
        var sparkleContainer = document.getElementById('sparkle-effects-container');
        if (!sparkleContainer) {
          sparkleContainer = document.createElement('div');
          sparkleContainer.id = 'sparkle-effects-container';
          sparkleContainer.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 9997;';
          document.body.appendChild(sparkleContainer);
        }
        loadParticleEffect('sparkle-effects-container', {
          particles: {
            number: { value: scaleParticleCount(20, 8), density: { enable: true, area: 800 } },
            color: { value: ['#ffd700', '#ffff00', '#fff68f', '#fffacd'] },
            shape: {
              type: 'char',
              options: {
                char: {
                  value: ['✨', '⭐', '✦', '✧'],
                  font: 'Segoe UI Emoji',
                  weight: 400
                }
              }
            },
            opacity: {
              value: { min: 0.2, max: 1 },
              animation: { enable: true, speed: 1.5, minimumValue: 0.1, sync: false }
            },
            size: { value: { min: 6, max: 12 } },
            move: {
              enable: true,
              speed: 0.15,
              direction: 'none',
              outModes: { default: 'bounce' },
              random: true
            }
          },
          interactivity: { events: { onClick: { enable: false }, onHover: { enable: false } } },
          background: { color: 'transparent' }
        });
      }
    });
  }

  function init() {
    var effect = window.DYNAMIC_EFFECT;

    switch (effect) {
      case 'snowflakes':
        initSnowflakes();
        break;
      case 'snow':
        initSnow();
        break;
      case 'cherry_blossoms':
        initCherryBlossoms();
        break;
      case 'red_poinciana':
        initRedPoinciana();
        break;
      case 'rain':
        initRain();
        break;
      case 'fireflies':
        initFireflies();
        break;
      case 'lunar_new_year':
        initLunarNewYear();
        break;
      default:
        console.warn('Unknown effect:', effect);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
