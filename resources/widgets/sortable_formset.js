/**
 * Sortable Formset Widget
 *
 * Provides drag-drop reordering and dynamic add/remove for Django formsets.
 *
 * Usage:
 *   Automatically initializes on elements with class "sortable-formset"
 *   Or manually: new SortableFormset(containerElement);
 */

class SortableFormset {
  constructor(container) {
    this.container = container;
    this.prefix = container.dataset.prefix;
    this.orderField = container.dataset.orderField || 'order';
    this.tbody = container.querySelector('.sortable-body');
    this.totalFormsInput = container.querySelector(`[name="${this.prefix}-TOTAL_FORMS"]`);

    this.init();
  }

  init() {
    this.initSortable();
    this.initAddRow();
    this.initRemoveRow();
    this.updateRowNumbers();
  }

  initSortable() {
    if (!this.tbody || typeof $ === 'undefined' || !$.fn.sortable) {
      console.warn('SortableFormset: jQuery UI sortable not available');
      return;
    }

    const self = this;
    $(this.tbody).sortable({
      handle: '.drag-handle',
      items: '.sortable-row:not(.sortable-template):visible',
      axis: 'y',
      cursor: 'grabbing',
      placeholder: 'sortable-placeholder',
      helper: function(e, tr) {
        // Preserve cell widths during drag
        const $originals = tr.children();
        const $helper = tr.clone();
        $helper.children().each(function(index) {
          $(this).width($originals.eq(index).width());
        });
        return $helper;
      },
      update: function() {
        self.updateOrder();
        self.updateRowNumbers();
      }
    });
  }

  initAddRow() {
    const addBtn = this.container.querySelector('.add-row-btn');
    if (!addBtn) return;

    addBtn.addEventListener('click', () => this.addRow());
  }

  initRemoveRow() {
    // Use event delegation for remove buttons
    this.container.addEventListener('click', (e) => {
      const removeBtn = e.target.closest('.remove-row-btn');
      if (removeBtn) {
        this.removeRow(removeBtn.closest('.sortable-row'));
      }
    });
  }

  addRow() {
    // Find the template row (has __prefix__ in its inputs)
    const templateRow = this.tbody.querySelector('.sortable-template');
    if (!templateRow) {
      console.warn('SortableFormset: No template row found');
      return;
    }

    const totalForms = parseInt(this.totalFormsInput.value);
    const newRow = templateRow.cloneNode(true);

    // Replace __prefix__ with the new index
    newRow.innerHTML = newRow.innerHTML.replace(/__prefix__/g, totalForms);
    newRow.classList.remove('sortable-template');
    newRow.style.display = '';
    newRow.dataset.rowIndex = totalForms;

    // Copy hidden field values from an existing row (except id, DELETE, and order)
    // This ensures fields like 'lesson' get the correct value
    this.copyHiddenFieldDefaults(newRow);

    // Insert before the template row
    templateRow.parentNode.insertBefore(newRow, templateRow);

    // Update total forms count
    this.totalFormsInput.value = totalForms + 1;

    // Show the table if it was hidden (no initial rows)
    const table = this.container.querySelector('.sortable-table');
    if (table && table.style.display === 'none') {
      table.style.display = '';
    }

    // Initialize any widgets on the new row (Select2, etc.)
    this.initWidgets(newRow);

    // Update order and row numbers
    this.updateOrder();
    this.updateRowNumbers();

    // Scroll to the new row
    newRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  copyHiddenFieldDefaults(newRow) {
    // Find the first existing visible row to copy hidden field values from
    const existingRow = this.tbody.querySelector('.sortable-row:not(.sortable-template):not(.sortable-deleted)');

    if (existingRow) {
      // Copy from existing row
      const existingHiddenInputs = existingRow.querySelectorAll('input[type="hidden"]');

      existingHiddenInputs.forEach(existingInput => {
        // Extract the field name (last part after the last dash)
        const nameParts = existingInput.name.split('-');
        const fieldName = nameParts[nameParts.length - 1];

        // Skip id, DELETE, and order fields - these should be unique per row
        if (fieldName === 'id' || fieldName === 'DELETE' || fieldName === this.orderField) {
          return;
        }

        // Find the corresponding hidden input in the new row
        const newInput = newRow.querySelector(`input[type="hidden"][name$="-${fieldName}"]`);
        if (newInput && !newInput.value && existingInput.value) {
          // Copy the value only if the new input is empty
          newInput.value = existingInput.value;
        }
      });
    } else {
      // No existing row - try to extract lesson ID from the prefix
      // Prefix format is like "problems_98" or "quizzes_98" where 98 is the lesson ID
      const prefixMatch = this.prefix.match(/^(?:problems|quizzes)_(\d+)$/);
      if (prefixMatch) {
        const lessonId = prefixMatch[1];
        const lessonInput = newRow.querySelector('input[type="hidden"][name$="-lesson"]');
        if (lessonInput && !lessonInput.value) {
          lessonInput.value = lessonId;
        }
      }
    }
  }

  removeRow(row) {
    if (!row || row.classList.contains('sortable-template')) return;

    // Find the DELETE checkbox and check it
    const deleteCheckbox = row.querySelector('input[name$="-DELETE"]');
    if (deleteCheckbox) {
      deleteCheckbox.checked = true;
      // Hide the row instead of removing it (Django needs the DELETE field)
      row.style.display = 'none';
      row.classList.add('sortable-deleted');
    } else {
      // If no delete checkbox (new row not yet saved), just remove it
      row.remove();
      // Decrement total forms only for truly new rows
      const totalForms = parseInt(this.totalFormsInput.value);
      if (totalForms > 0) {
        this.totalFormsInput.value = totalForms - 1;
      }
    }

    this.updateRowNumbers();
  }

  updateOrder() {
    const rows = this.tbody.querySelectorAll('.sortable-row:not(.sortable-template):not(.sortable-deleted)');
    rows.forEach((row, index) => {
      const orderInput = row.querySelector(`input[name$="-${this.orderField}"]`);
      if (orderInput) {
        orderInput.value = index + 1;
      }
    });
  }

  updateRowNumbers() {
    const rows = this.tbody.querySelectorAll('.sortable-row:not(.sortable-template)');
    let visibleIndex = 0;
    rows.forEach((row) => {
      if (row.style.display !== 'none' && !row.classList.contains('sortable-deleted')) {
        visibleIndex++;
        const numberSpan = row.querySelector('.row-number');
        if (numberSpan) {
          numberSpan.textContent = visibleIndex;
        }
      }
    });
  }

  initWidgets(row) {
    // Initialize Select2 on any select elements
    if (typeof $ !== 'undefined' && $.fn.select2) {
      $(row).find('select').each(function() {
        const $select = $(this);

        // When cloning template rows, the select may have select2 classes but no actual
        // select2 data attached. We need to clean up and reinitialize.
        if ($select.hasClass('select2-hidden-accessible')) {
          // Remove the old select2 container (sibling element)
          $select.next('.select2-container').remove();
          // Remove the class so select2 can reinitialize
          $select.removeClass('select2-hidden-accessible');
        }

        // Use django-select2's djangoSelect2 if available (handles heavy/AJAX selects properly)
        if ($.fn.djangoSelect2) {
          $select.djangoSelect2({
            dropdownAutoWidth: true
          });
        } else {
          // Fallback to regular select2
          $select.select2({
            width: '100%'
          });
        }
      });
    }
  }
}

// Auto-initialize on DOM ready
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.sortable-formset').forEach(function(container) {
    new SortableFormset(container);
  });
});

// Export for manual initialization
if (typeof window !== 'undefined') {
  window.SortableFormset = SortableFormset;
}
