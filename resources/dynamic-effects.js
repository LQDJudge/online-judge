(function() {
  'use strict';

  // Only run on desktop
  if (window.innerWidth < 800) {
    return;
  }

  if (!window.DYNAMIC_EFFECT || window.DYNAMIC_EFFECT === 'none') {
    return;
  }

  // Create container for effects
  function createContainer() {
    var container = document.getElementById('dynamic-effects-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'dynamic-effects-container';
      container.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 9999;';
      document.body.appendChild(container);
    }
    return container;
  }

  // Load tsParticles for effects
  function loadTsParticles(callback) {
    var script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/tsparticles@2/tsparticles.bundle.min.js';
    script.onload = callback;
    document.head.appendChild(script);
  }

  // Snowflakes effect using tsParticles (blue snowflakes, gentle floating like blossoms)
  function initSnowflakes() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 25, density: { enable: true, area: 800 } },
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
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 60, density: { enable: true, area: 800 } },
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
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 20, density: { enable: true, area: 800 } },
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

  // Rain effect (falling raindrops - thin lines)
  function initRain() {
    loadTsParticles(function() {
      if (typeof tsParticles !== 'undefined') {
        createContainer();
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 150, density: { enable: true, area: 800 } },
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
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 40, density: { enable: true, area: 800 } },
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
              blur: 15
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
        tsParticles.load('dynamic-effects-container', {
          particles: {
            number: { value: 30, density: { enable: true, area: 800 } },
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
        tsParticles.load('lantern-effects-container', {
          particles: {
            number: { value: 5, density: { enable: true, area: 1000 } },
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
        tsParticles.load('sparkle-effects-container', {
          particles: {
            number: { value: 20, density: { enable: true, area: 800 } },
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
