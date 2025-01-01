var DjangoPagedown = DjangoPagedown || {};

DjangoPagedown = (function () {
  const converter = Markdown.getSanitizingConverter();
  const editors = {};

  Markdown.Extra.init(converter, {
    extensions: 'all'
  });

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
      const editor = new Markdown.Editor(converter, id, {});

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
    if (elements.length > 0) {
      createEditor(elements[0]); // Only handle the first element
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
    }
  };
})();

window.onload = DjangoPagedown.init;