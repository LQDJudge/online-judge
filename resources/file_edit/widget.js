$(document).ready(function () {
  // Function to determine Ace Editor mode based on file extension
  const setMode = (filename) => {
    const extension = filename.split('.').pop().toLowerCase();
    const modes = {
      js: 'ace/mode/javascript',
      json: 'ace/mode/json',
      html: 'ace/mode/html',
      css: 'ace/mode/css',
      py: 'ace/mode/python',
      java: 'ace/mode/java',
      cpp: 'ace/mode/c_cpp',
      txt: 'ace/mode/text',
      md: 'ace/mode/markdown',
      xml: 'ace/mode/xml',
    };
    return modes[extension] || 'ace/mode/text'; // Default to plain text
  };

  // Function to initialize the Ace Editor
  const initializeEditor = (elementId, content, filename) => {
    const uniqueEditorId = `fl-ace-editor-${elementId}`;
    const $editor = $(`.featherlight #file-editor-modal-${elementId} .fl-ace-editor`);
    $editor.attr('id', uniqueEditorId); // Assign unique ID to the editor container

    $(`.featherlight #filename-input-${elementId}`).val(filename);
    $(`.featherlight #file-editor-modal-${elementId}`).css('display', 'block');

    const flAceEditor = ace.edit(uniqueEditorId);
    flAceEditor.setTheme('ace/theme/monokai');
    flAceEditor.session.setMode(setMode(filename)); // Set mode based on filename
    flAceEditor.setValue(String(content), -1); // Set content in the editor
    flAceEditor.resize();

    return flAceEditor;
  };

  // Function to handle modal opening with Ace Editor
  const openModalWithEditor = (elementId, content, filename) => {
    const $modal = $(`#file-editor-modal-${elementId}`);
    $.featherlight($modal, {
      clone: true,
      afterOpen: function () {
        const editor = initializeEditor(elementId, content, filename);
        $modal.data('editor', editor); // Store the editor instance
      },
      afterClose: function () {
        $modal.css('display', 'none');
      },
    });
  };

  // Event handler for the "Edit" button
  $(document).on('click', '.edit-file-btn', function () {
    const elementId = $(this).data('element-id');
    const fileInput = $(`#${elementId}`);
    const fileContent = $(`#file-editor-container-${elementId}`).data('file-content') || '';
    const dbFileName = $(`#file-editor-container-${elementId}`).data('file-name') || '';
    const defaultFileName = $(`#file-editor-container-${elementId}`).data('default-file-name') || 'new_file.txt';

    if (fileInput[0].files.length > 0) {
      const reader = new FileReader();
      reader.onload = function (e) {
        openModalWithEditor(elementId, e.target.result, fileInput[0]?.files[0]?.name || dbFileName || defaultFileName);
      };
      reader.readAsText(fileInput[0].files[0]);
    } else {
      openModalWithEditor(elementId, fileContent, dbFileName || defaultFileName);
    }
  });

  // Event handler for the "Save" button
  $(document).on('click', '.save-file-btn', function () {
    const elementId = $(this).data('element-id');
    const fileInput = $(`#${elementId}`);
    const $modal = $(`#file-editor-modal-${elementId}`);
    const editor = $modal.data('editor');

    // Get the updated content and filename
    const updatedContent = editor.getValue();
    const updatedFilename = $(`.featherlight #filename-input-${elementId}`).val().trim() || 'new_file.txt'; // Default to 'new_file.txt' if empty

    const fileType = fileInput[0]?.files[0]?.type || 'text/plain';

    const blob = new Blob([updatedContent], { type: fileType });
    const newFile = new File([blob], updatedFilename, { type: fileType });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(newFile);
    fileInput[0].files = dataTransfer.files;

    $.featherlight.current().close();
  });
});