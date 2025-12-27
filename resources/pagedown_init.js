var DjangoPagedown = DjangoPagedown || {};

DjangoPagedown = (function () {
  let converter = null;
  const editors = {};

  // Lazy-initialize converter (shared across all editors)
  const getConverter = function () {
    if (!converter) {
      converter = Markdown.getSanitizingConverter();
      Markdown.Extra.init(converter, {
        extensions: 'all'
      });
    }
    return converter;
  };

  // Function to handle clipboard image uploads
  const registerClipboardUpload = function (element) {
    element.addEventListener('paste', function (event) {
      const clipboardData = event.clipboardData || window.clipboardData;
      const items = clipboardData.items;

      for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          const formData = new FormData();
          formData.append('image', blob);

          element.disabled = true;

          const xhr = new XMLHttpRequest();
          xhr.open('POST', '/pagedown/image-upload/', true);

          xhr.onload = function () {
            element.disabled = false;
            element.focus();

            if (xhr.status === 200) {
              const response = JSON.parse(xhr.responseText);
              const imageUrl = response.url;
              let currentMarkdown = element.value;
              let markdownImageText = `![](${imageUrl})`;

              const start = element.selectionStart;
              const end = element.selectionEnd;
              const currentValue = element.value;
              let startValue = currentValue.slice(0, start);
              let endValue = currentValue.slice(end);

              if (startValue) startValue += "\n";
              if (endValue) markdownImageText += "\n";
              element.value = startValue + markdownImageText + endValue;

              // Move the cursor to just after the inserted text
              const newCursorPosition = start + markdownImageText.length;
              element.setSelectionRange(newCursorPosition, newCursorPosition);
            } else {
              alert('There was an error uploading the image.');
            }
          };

          xhr.onerror = function () {
            element.disabled = false;
            alert('There was an error uploading the image.');
          };

          xhr.send(formData);

          // Only handle the first image in the clipboard data
          break;
        }
      }
    });
  };

  const createEditor = function (element) {
    const input = element.getElementsByClassName('wmd-input')[0];
    if (!input) return;

    const id = input.id.substr(9);
    if (!editors.hasOwnProperty(id)) {
      const editor = new Markdown.Editor(getConverter(), id, {});

      // Handle image upload dialog
      if (element.classList.contains('image-upload-enabled')) {
        const upload = element.getElementsByClassName('pagedown-image-upload')[0];
        const url = upload.getElementsByClassName('url-input')[0];
        const file = upload.getElementsByClassName('file-input')[0];
        const cancel = upload.getElementsByClassName('deletelink')[0];
        const submit = upload.getElementsByClassName('submit-input')[0];
        const loading = upload.getElementsByClassName('submit-loading')[0];

        const close = function (value, callback = undefined) {
          upload.classList.remove('show');
          url.value = '';
          file.value = '';
          document.removeEventListener('click', outsideClickListener);
          if (callback) callback(value);
        };

        const outsideClickListener = function (event) {
          if (!upload.contains(event.target) && upload.classList.contains('show')) {
            cancel.click();
          }
        };

        editor.hooks.set('insertImageDialog', function (callback) {
          upload.classList.add('show');

          setTimeout(function () {
            document.addEventListener('click', outsideClickListener);
          }, 0);

          cancel.addEventListener(
            'click',
            function (event) {
              close(null, callback);
              event.preventDefault();
            },
            { once: true }
          );

          submit.addEventListener(
            'click',
            function (event) {
              if (url.value.length > 0) {
                close(url.value, callback);
              } else if (file.files.length > 0) {
                loading.classList.add('show');
                submit.classList.remove('show');

                const data = new FormData();
                const xhr = new XMLHttpRequest();
                data.append('image', file.files[0]);
                xhr.open('POST', file.dataset.action, true);

                xhr.onload = function () {
                  loading.classList.remove('show');
                  submit.classList.add('show');

                  if (xhr.status !== 200) {
                    alert(xhr.statusText);
                  } else {
                    const response = JSON.parse(xhr.response);
                    if (response.success) {
                      close(response.url, callback);
                    } else {
                      if (response.error) {
                        let error = '';
                        for (const key in response.error) {
                          if (response.error.hasOwnProperty(key)) {
                            error += `${key}: ${response.error[key]}`;
                          }
                        }
                        alert(error);
                      }
                      close(null, callback);
                    }
                  }
                };

                xhr.onerror = function () {
                  alert('Upload failed.');
                };

                xhr.send(data);
              } else {
                close(null, callback);
              }
              event.preventDefault();
            },
            { once: true }
          );

          return true;
        });
      }

      // Register clipboard paste event for image upload
      registerClipboardUpload(input);

      editor.run();
      editors[id] = editor;
    }
  };

  const destroyEditor = function (element) {
    const input = element.getElementsByClassName('wmd-input')[0];
    if (!input) return;

    const id = input.id.substr(9);
    if (editors.hasOwnProperty(id)) {
      delete editors[id];
      return true;
    }
    return false;
  };

  const init = function () {
    const elements = document.getElementsByClassName('wmd-wrapper');
    for (let i = 0; i < elements.length; i++) {
      createEditor(elements[i]); // Handle all elements
    }
  };

  return {
    init: function () {
      return init();
    },
    createEditor: function (element) {
      return createEditor(element);
    },
    destroyEditor: function (element) {
      return destroyEditor(element);
    },
    getConverter: getConverter
  };
})();

window.onload = DjangoPagedown.init;

/**
 * MarkdownEditor - A reusable markdown editor component
 *
 * Encapsulates PageDown editor, preview functionality, and image upload
 * into a single, easy-to-use class. Use this for dynamically created editors
 * (like comment reply forms) instead of cloning existing DOM elements.
 *
 * Usage:
 *   const editor = new MarkdownEditor(containerElement, {
 *     id: 'my-editor',
 *     previewUrl: '/preview/',
 *     imageUploadUrl: '/upload/',
 *     previewTimeout: 500,
 *     onChange: (value) => console.log(value),
 *     onSubmit: () => form.submit()
 *   });
 *
 *   editor.getValue();
 *   editor.setValue('# Hello');
 *   editor.focus();
 *   editor.destroy();
 */
class MarkdownEditor {
  static instanceCount = 0;
  static instances = new Map();

  /**
   * Create a new MarkdownEditor
   * @param {HTMLElement} container - The container element to render the editor in
   * @param {Object} options - Configuration options
   */
  constructor(container, options = {}) {
    this.id = options.id || `markdown-editor-${++MarkdownEditor.instanceCount}`;
    this.container = container;
    this.options = {
      previewUrl: '/pagedown/preview/',
      imageUploadUrl: '/pagedown/image-upload/',
      previewTimeout: 500,
      enableImageUpload: true,
      placeholder: '',
      initialValue: '',
      onChange: null,
      onSubmit: null,
      onCancel: null,
      mode: 'full', // 'minimal' or 'full'
      submitLabel: 'Reply', // Button label: 'Reply' for replies, 'Post' for new comments
      cancelLabel: 'Cancel',
      expandLabel: 'Expand editor',
      collapseLabel: 'Collapse editor',
      ...options
    };

    this.editor = null;
    this.previewTimeout = null;
    this.lastText = '';
    this.mode = this.options.mode;

    if (this.mode === 'minimal') {
      this.renderMinimal();
    } else {
      this.renderFull();
      this.initEditor();
      this.initPreview();
    }

    this.initImageUpload();
    this.initKeyboardShortcuts();

    MarkdownEditor.instances.set(this.id, this);
  }

  /**
   * Generate unique element IDs
   */
  getIds() {
    return {
      wrapper: `wmd-wrapper-${this.id}`,
      buttonBar: `wmd-button-bar-${this.id}`,
      input: `wmd-input-${this.id}`,
      preview: `${this.id}-preview`,
      previewContent: `${this.id}-preview-content`,
      previewUpdate: `${this.id}-preview-update`,
      imageUpload: `${this.id}-image-upload`,
    };
  }

  /**
   * Render minimal mode - YouTube-style comment input
   */
  renderMinimal() {
    const ids = this.getIds();

    this.container.innerHTML = `
      <div id="${ids.wrapper}" class="wmd-wrapper wmd-wrapper-minimal${this.options.enableImageUpload ? ' image-upload-enabled' : ''}">
        <div class="minimal-editor-container">
          <textarea
            id="${ids.input}"
            class="wmd-input minimal-input"
            placeholder="${this.options.placeholder || 'Write a comment...'}"
            rows="1"
          >${this.options.initialValue}</textarea>
          <div class="minimal-actions">
            <button type="button" class="minimal-expand-btn" title="${this.options.expandLabel}">
              <i class="fa fa-expand"></i>
            </button>
            <button type="button" class="minimal-cancel-btn">${this.options.cancelLabel}</button>
            <button type="button" class="minimal-submit-btn">${this.options.submitLabel}</button>
          </div>
        </div>
      </div>
    `;

    // Cache element references
    this.elements = {
      wrapper: document.getElementById(ids.wrapper),
      input: document.getElementById(ids.input),
      expandBtn: this.container.querySelector('.minimal-expand-btn'),
      cancelBtn: this.container.querySelector('.minimal-cancel-btn'),
      submitBtn: this.container.querySelector('.minimal-submit-btn'),
    };

    this.lastText = this.options.initialValue;

    // Set up focus/blur handlers to show/hide actions
    this.elements.input.addEventListener('focus', () => {
      this.elements.wrapper.classList.add('has-focus');
    });

    this.elements.input.addEventListener('blur', () => {
      // Delay to allow button clicks to register
      setTimeout(() => {
        // Check if editor was destroyed during the delay
        if (!this.elements?.input) return;
        if (!this.elements.input.value.trim()) {
          this.elements.wrapper.classList.remove('has-focus');
        }
      }, 150);
    });

    // Update submit button state based on content
    this.elements.input.addEventListener('input', () => {
      if (this.elements.input.value.trim()) {
        this.elements.wrapper.classList.add('has-content');
        this.elements.submitBtn.classList.add('active');
      } else {
        this.elements.wrapper.classList.remove('has-content');
        this.elements.submitBtn.classList.remove('active');
      }
    });

    // Set up expand button click handler
    this.elements.expandBtn.addEventListener('click', (e) => {
      e.preventDefault();
      this.expand();
    });

    // Set up cancel button handler
    this.elements.cancelBtn.addEventListener('click', (e) => {
      e.preventDefault();
      this.clear();
      this.elements.wrapper.classList.remove('has-focus', 'has-content');
      this.elements.submitBtn.classList.remove('active');
      this.elements.input.blur();
      // Trigger cancel callback if provided
      if (typeof this.options.onCancel === 'function') {
        this.options.onCancel();
      }
    });

    // Set up submit button handler
    this.elements.submitBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (this.elements.input.value.trim() && typeof this.options.onSubmit === 'function') {
        this.options.onSubmit();
      }
    });

    // Set up auto-resize for textarea
    this.initAutoResize();

    // Initialize state if there's initial content
    if (this.options.initialValue) {
      this.elements.wrapper.classList.add('has-content');
      this.elements.submitBtn.classList.add('active');
    }
  }

  /**
   * Initialize auto-resize functionality for textarea
   */
  initAutoResize() {
    const textarea = this.elements.input;
    if (!textarea) return;

    const autoResize = () => {
      // Reset height to calculate scrollHeight correctly
      textarea.style.height = 'auto';
      // Set height to scrollHeight (content height)
      const newHeight = Math.min(textarea.scrollHeight, 200); // max-height from CSS
      textarea.style.height = newHeight + 'px';
    };

    // Listen for input events
    textarea.addEventListener('input', autoResize);

    // Initial resize if there's content
    if (this.options.initialValue) {
      autoResize();
    }

    // Store the handler for cleanup
    this._autoResizeHandler = autoResize;
  }

  /**
   * Render full mode - complete PageDown editor with toolbar and preview
   */
  renderFull() {
    const ids = this.getIds();
    const currentValue = this.elements?.input?.value || this.options.initialValue;

    this.container.innerHTML = `
      <div id="${ids.wrapper}" class="wmd-wrapper${this.options.enableImageUpload ? ' image-upload-enabled' : ''}">
        <div class="full-editor-header">
          <div id="${ids.buttonBar}" class="wmd-button-bar"></div>
          <button type="button" class="full-collapse-btn" title="${this.options.collapseLabel}">
            <i class="fa fa-compress"></i>
          </button>
        </div>
        <textarea
          id="${ids.input}"
          class="wmd-input"
          placeholder="${this.options.placeholder}"
        >${currentValue}</textarea>

        ${this.options.enableImageUpload ? this.getImageUploadTemplate(ids) : ''}

        <div id="wmd-preview-${this.id}" style="display:none;"></div>

        <div id="${ids.preview}"
             class="wmd-panel wmd-preview dmmd-preview"
             data-preview-url="${this.options.previewUrl}"
             data-textarea-id="${ids.input}"
             data-timeout="${this.options.previewTimeout}">
          <div id="${ids.previewUpdate}" class="dmmd-preview-update">
            <i class="fa fa-refresh"></i> Preview
          </div>
          <div id="${ids.previewContent}" class="dmmd-preview-content content-description"></div>
        </div>

        ${this.options.onSubmit ? `
        <div class="full-editor-actions">
          <button type="button" class="full-cancel-btn">${this.options.cancelLabel}</button>
          <button type="button" class="full-submit-btn">${this.options.submitLabel}</button>
        </div>
        ` : ''}
      </div>
    `;

    // Cache element references
    this.elements = {
      wrapper: document.getElementById(ids.wrapper),
      buttonBar: document.getElementById(ids.buttonBar),
      input: document.getElementById(ids.input),
      preview: document.getElementById(ids.preview),
      previewContent: document.getElementById(ids.previewContent),
      previewUpdate: document.getElementById(ids.previewUpdate),
      imageUpload: document.getElementById(ids.imageUpload),
      collapseBtn: this.container.querySelector('.full-collapse-btn'),
      cancelBtn: this.container.querySelector('.full-cancel-btn'),
      submitBtn: this.container.querySelector('.full-submit-btn'),
    };

    this.lastText = currentValue;

    // Set up collapse button click handler (only if started in minimal mode)
    if (this.options.mode === 'minimal' && this.elements.collapseBtn) {
      this.elements.collapseBtn.addEventListener('click', (e) => {
        e.preventDefault();
        this.collapse();
      });
    } else if (this.elements.collapseBtn) {
      // Hide collapse button if editor was created in full mode
      this.elements.collapseBtn.style.display = 'none';
    }

    // Set up action button handlers for full mode
    if (this.elements.cancelBtn) {
      this.elements.cancelBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (typeof this.options.onCancel === 'function') {
          this.options.onCancel();
        } else {
          // Default behavior: reset to minimal mode
          this.reset();
        }
      });
    }

    if (this.elements.submitBtn) {
      this.elements.submitBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (this.getValue().trim() && typeof this.options.onSubmit === 'function') {
          this.options.onSubmit();
        }
      });
    }
  }

  /**
   * Expand from minimal to full mode
   */
  expand() {
    if (this.mode === 'full') return;

    const currentValue = this.elements.input.value;
    this.mode = 'full';

    this.renderFull();
    this.elements.input.value = currentValue;
    this.lastText = currentValue;

    this.initEditor();
    this.initPreview();
    this.initImageUpload();
    this.initKeyboardShortcuts();

    this.elements.input.focus();
  }

  /**
   * Collapse from full to minimal mode
   */
  collapse() {
    if (this.mode === 'minimal') return;

    const currentValue = this.elements.input.value;
    this.mode = 'minimal';

    // Clean up PageDown editor
    this.editor = null;

    this.renderMinimal();
    this.elements.input.value = currentValue;
    this.lastText = currentValue;

    this.initImageUpload();
    this.initKeyboardShortcuts();
  }

  /**
   * Get image upload dialog template
   */
  getImageUploadTemplate(ids) {
    return `
      <div id="${ids.imageUpload}" class="pagedown-image-upload">
        <div class="image-upload-content">
          <label>Image URL:</label>
          <input type="text" class="url-input" placeholder="https://...">
          <label>Or upload file:</label>
          <input type="file" class="file-input" accept="image/*" data-action="${this.options.imageUploadUrl}">
          <div class="image-upload-actions">
            <button type="button" class="submit-input show">Insert</button>
            <span class="submit-loading"><i class="fa fa-spinner fa-pulse"></i></span>
            <button type="button" class="deletelink">Cancel</button>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * Initialize the PageDown editor
   */
  initEditor() {
    // PageDown concatenates suffix directly (e.g., "wmd-input" + suffix)
    // So we need to include the hyphen in the suffix
    const suffix = `-${this.id}`;
    // Use shared converter from DjangoPagedown
    this.editor = new Markdown.Editor(DjangoPagedown.getConverter(), suffix, {});

    // Set up image upload dialog hook if enabled
    if (this.options.enableImageUpload && this.elements.imageUpload) {
      this.setupImageUploadDialog();
    }

    this.editor.run();
  }

  /**
   * Set up the image upload dialog for PageDown
   */
  setupImageUploadDialog() {
    const upload = this.elements.imageUpload;
    const url = upload.querySelector('.url-input');
    const file = upload.querySelector('.file-input');
    const cancel = upload.querySelector('.deletelink');
    const submit = upload.querySelector('.submit-input');
    const loading = upload.querySelector('.submit-loading');

    let outsideClickListener = null;

    const close = (value, callback) => {
      upload.classList.remove('show');
      url.value = '';
      file.value = '';
      if (outsideClickListener) {
        document.removeEventListener('click', outsideClickListener);
      }
      if (callback) callback(value);
    };

    this.editor.hooks.set('insertImageDialog', (callback) => {
      upload.classList.add('show');

      outsideClickListener = (event) => {
        if (!upload.contains(event.target) && upload.classList.contains('show')) {
          cancel.click();
        }
      };

      setTimeout(() => {
        document.addEventListener('click', outsideClickListener);
      }, 0);

      const cancelHandler = (event) => {
        close(null, callback);
        event.preventDefault();
      };

      const submitHandler = (event) => {
        if (url.value.length > 0) {
          close(url.value, callback);
        } else if (file.files.length > 0) {
          loading.classList.add('show');
          submit.classList.remove('show');

          const data = new FormData();
          data.append('image', file.files[0]);

          fetch(file.dataset.action, {
            method: 'POST',
            body: data
          })
          .then(response => response.json())
          .then(result => {
            loading.classList.remove('show');
            submit.classList.add('show');

            if (result.success) {
              close(result.url, callback);
            } else {
              if (result.error) {
                alert(Object.entries(result.error).map(([k, v]) => `${k}: ${v}`).join('\n'));
              }
              close(null, callback);
            }
          })
          .catch(() => {
            loading.classList.remove('show');
            submit.classList.add('show');
            alert('Upload failed.');
            close(null, callback);
          });
        } else {
          close(null, callback);
        }
        event.preventDefault();
      };

      cancel.addEventListener('click', cancelHandler, { once: true });
      submit.addEventListener('click', submitHandler, { once: true });

      return true;
    });
  }

  /**
   * Initialize preview functionality
   */
  initPreview() {
    const { input, preview, previewContent, previewUpdate } = this.elements;

    const updatePreview = () => {
      const text = input.value;
      if (text) {
        preview.classList.add('dmmd-preview-stale');

        fetch(this.options.previewUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
          },
          body: new URLSearchParams({
            preview: text,
            csrfmiddlewaretoken: this.getCsrfToken()
          })
        })
        .then(response => response.text())
        .then(html => {
          previewContent.innerHTML = html;
          preview.classList.add('dmmd-preview-has-content');
          preview.classList.remove('dmmd-preview-stale');

          // Render math if available
          if (typeof renderKatex === 'function') {
            renderKatex(previewContent);
          }
        });
      } else {
        previewContent.innerHTML = '';
        preview.classList.remove('dmmd-preview-has-content', 'dmmd-preview-stale');
      }
    };

    // Click to update preview
    previewUpdate.addEventListener('click', updatePreview);

    // Auto-update preview on input with debounce
    input.addEventListener('input', () => {
      const text = input.value;
      if (this.lastText === text) return;
      this.lastText = text;

      preview.classList.add('dmmd-preview-stale');

      if (this.previewTimeout) {
        clearTimeout(this.previewTimeout);
      }

      this.previewTimeout = setTimeout(() => {
        updatePreview();
        this.previewTimeout = null;
      }, this.options.previewTimeout);

      // Call onChange callback if provided
      if (typeof this.options.onChange === 'function') {
        this.options.onChange(text);
      }
    });

    // Initial preview if there's content
    if (this.options.initialValue) {
      updatePreview();
    }
  }

  /**
   * Initialize clipboard image paste
   */
  initImageUpload() {
    if (!this.options.enableImageUpload) return;

    const input = this.elements.input;

    input.addEventListener('paste', (event) => {
      const clipboardData = event.clipboardData || window.clipboardData;
      const items = clipboardData.items;

      for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          const formData = new FormData();
          formData.append('image', blob);

          input.disabled = true;

          fetch(this.options.imageUploadUrl, {
            method: 'POST',
            body: formData
          })
          .then(response => response.json())
          .then(result => {
            input.disabled = false;
            input.focus();

            if (result.url) {
              const imageMarkdown = `![](${result.url})`;
              const start = input.selectionStart;
              const end = input.selectionEnd;
              let before = input.value.slice(0, start);
              let after = input.value.slice(end);

              if (before && !before.endsWith('\n')) before += '\n';
              if (after && !after.startsWith('\n')) after = '\n' + after;

              input.value = before + imageMarkdown + after;

              const newPos = before.length + imageMarkdown.length;
              input.setSelectionRange(newPos, newPos);

              // Trigger input event for preview update
              input.dispatchEvent(new Event('input'));
            }
          })
          .catch(() => {
            input.disabled = false;
            alert('Error uploading image.');
          });

          break;
        }
      }
    });
  }

  /**
   * Initialize keyboard shortcuts
   */
  initKeyboardShortcuts() {
    const input = this.elements.input;

    input.addEventListener('keydown', (event) => {
      // Ctrl+Enter or Cmd+Enter to submit
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        if (typeof this.options.onSubmit === 'function') {
          event.preventDefault();
          this.options.onSubmit();
        }
      }
    });
  }

  /**
   * Get CSRF token from cookie
   */
  getCsrfToken() {
    const name = 'csrftoken';
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
      cookie = cookie.trim();
      if (cookie.startsWith(name + '=')) {
        return cookie.substring(name.length + 1);
      }
    }
    return '';
  }

  /**
   * Get the current editor value
   * @returns {string}
   */
  getValue() {
    if (!this.elements?.input) return '';
    return this.elements.input.value;
  }

  /**
   * Set the editor value
   * @param {string} value
   */
  setValue(value) {
    if (!this.elements?.input) return;
    this.elements.input.value = value;
    this.lastText = value;
    this.elements.input.dispatchEvent(new Event('input'));
  }

  /**
   * Clear the editor
   */
  clear() {
    this.setValue('');
  }

  /**
   * Reset the editor to initial minimal state
   * - Collapses to minimal mode if in full mode
   * - Clears the text
   * - Removes focus state
   * - Resets height
   */
  reset() {
    // Collapse to minimal mode if currently in full mode
    if (this.mode === 'full' && this.options.mode === 'minimal') {
      this.mode = 'minimal';
      this.editor = null;
      this.renderMinimal();
      this.initImageUpload();
      this.initKeyboardShortcuts();
    }

    // Clear the text
    if (this.elements?.input) {
      this.elements.input.value = '';
      this.elements.input.style.height = 'auto';
      this.lastText = '';
    }

    // Remove focus state classes
    if (this.elements?.wrapper) {
      this.elements.wrapper.classList.remove('has-focus', 'has-content');
    }

    // Remove active state from submit button
    if (this.elements?.submitBtn) {
      this.elements.submitBtn.classList.remove('active');
    }

    // Blur the input
    if (this.elements?.input) {
      this.elements.input.blur();
    }
  }

  /**
   * Focus the editor
   */
  focus() {
    if (!this.elements?.input) return;
    this.elements.input.focus();
  }

  /**
   * Disable the editor
   */
  disable() {
    if (!this.elements?.input) return;
    this.elements.input.disabled = true;
  }

  /**
   * Enable the editor
   */
  enable() {
    if (!this.elements?.input) return;
    this.elements.input.disabled = false;
  }

  /**
   * Destroy the editor and clean up
   */
  destroy() {
    if (this.previewTimeout) {
      clearTimeout(this.previewTimeout);
    }

    // Remove from instances map
    MarkdownEditor.instances.delete(this.id);

    // Clear container
    this.container.innerHTML = '';

    // Clear references
    this.editor = null;
    this.elements = null;
  }

  /**
   * Get an editor instance by ID
   * @param {string} id
   * @returns {MarkdownEditor|undefined}
   */
  static getInstance(id) {
    return MarkdownEditor.instances.get(id);
  }

  /**
   * Destroy all editor instances
   */
  static destroyAll() {
    MarkdownEditor.instances.forEach(editor => editor.destroy());
  }
}

// Make available globally
window.MarkdownEditor = MarkdownEditor;
