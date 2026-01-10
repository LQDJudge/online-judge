/**
 * Problem Form JavaScript Utilities
 * Shared functions for problem add/edit forms
 */

/**
 * Initialize tab navigation system
 * Handles Content/Metadata tab switching
 */
function initTabs() {
    // Hide all tab panes except the first one
    $('.tab-pane').hide();
    $('.tab-pane.active').show();
    
    // Tab click handler
    $('.tab-button').off('click').on('click', function(e) {
        e.preventDefault();
        
        var $clickedTab = $(this);
        var targetPane = $clickedTab.attr('data-target');
                
        // Remove active class from all tabs and hide all panes
        $('.tab-button').removeClass('active');
        $('.tab-pane').removeClass('active').hide();
        
        // Add active class to clicked tab and show target pane
        $clickedTab.addClass('active');
        $(targetPane).addClass('active').show();
    });
}

/**
 * Update tab error highlighting
 * Highlights tabs that contain form errors
 */
function updateTabErrors() {
    $('.tab-pane').each(function() {
        var $pane = $(this);
        var hasErrors = $pane.find('.alert-danger').length > 0;
        
        var tabId = $pane.attr('id').replace('pane-', 'tab-');
        var $tabButton = $('#' + tabId);
        
        if (hasErrors) {
            $tabButton.addClass('has-errors');
        } else {
            $tabButton.removeClass('has-errors');
        }
    });

    // Auto-switch to first tab with errors (useful for edit forms)
    var $firstErrorTab = $('.tab-button.has-errors').first();
    if ($firstErrorTab.length > 0) {
        $firstErrorTab.click();
    }
}

/**
 * Setup memory unit conversion functionality
 * Converts between MB and KB when the unit is changed
 */
function setupMemoryUnitConversion() {
    var $memoryUnit = $('#id_memory_unit');
    var $memoryLimit = $('#id_memory_limit');
    
    if ($memoryUnit.length && $memoryLimit.length) {
      $memoryUnit.on('change', function() {
        var currentUnit = $(this).val();
        var currentValue = parseFloat($memoryLimit.val());
        
        if (!isNaN(currentValue)) {
          if (currentUnit === 'MB' && $memoryUnit.data('prev-unit') === 'KB') {
            $memoryLimit.val((currentValue / 1024).toFixed(0));
          } else if (currentUnit === 'KB' && $memoryUnit.data('prev-unit') === 'MB') {
            $memoryLimit.val(currentValue * 1024);
          }
        }
        $memoryUnit.data('prev-unit', currentUnit);
      });
      
      // Store initial unit
      $memoryUnit.data('prev-unit', $memoryUnit.val());
    }
}

/**
 * Setup "Select All Languages" checkbox functionality
 * Handles checking/unchecking all language checkboxes
 */
function setupSelectAllLanguages() {
    // Select All Languages Checkbox
    $('#select-all-languages').change(function() {
        $('#id_allowed_languages input[type="checkbox"]').prop('checked', $(this).prop('checked'));
    });

    // Update select all checkbox state based on individual language checkboxes
    $('#id_allowed_languages input[type="checkbox"]').change(function() {
        var allChecked = $('#id_allowed_languages input[type="checkbox"]:checked').length === 
                         $('#id_allowed_languages input[type="checkbox"]').length;
        $('#select-all-languages').prop('checked', allChecked);
    });
}

/**
 * Setup form handling to bypass HTML5 validation
 * Let Django server-side validation handle all errors properly
 */
function setupFormValidation() {
    // Disable HTML5 validation to let Django handle it
    $('form').attr('novalidate', 'novalidate');
}

/**
 * Standard problem description template
 */
var PROBLEM_TEMPLATE = `[Problem statement goes here. Describe the problem clearly.]

####Input
- [Describe input format]
- [Constraints]

####Output
- [Describe output format]

####Example

!!! question "Test 1"
    ???+ "Input"
        \`\`\`sample
        [input here]
        \`\`\`
    ???+ success "Output"
        \`\`\`sample
        [output here]
        \`\`\`
    ??? warning "Note"
        [Optional explanation]

####Scoring
- Subtask 1 (x points): $1 \\le n \\le 100$
- Subtask 2 (y points): $1 \\le n \\le 10^5$
`;

/**
 * Setup "Use Template" button functionality
 * @param {string} descriptionFieldId - The id_for_label of the description field
 * @param {string} confirmMessage - Translation for confirmation message
 */
function setupUseTemplateButton(descriptionFieldId, confirmMessage) {
    $('#use-template-btn').click(function() {
        // PageDown wraps the textarea with wmd-input-{id} format
        var $description = $('#wmd-input-' + descriptionFieldId);
        var currentValue = ($description.val() || '').trim();

        if (currentValue && !confirm(confirmMessage)) {
            return;
        }

        $description.val(PROBLEM_TEMPLATE);

        // Trigger input event to update PageDown preview
        $description.trigger('input').trigger('change');
    });
}

/**
 * Initialize all problem form utilities
 * Call this function to setup all shared functionality
 */
function initializeProblemForm() {
    // Initialize tab system
    initTabs();

    // Setup form functionality
    setupMemoryUnitConversion();
    setupSelectAllLanguages();
    setupFormValidation();

    // Setup error highlighting
    updateTabErrors();

    // Dynamically check for errors when inputs change
    $('.tab-pane input, .tab-pane textarea, .tab-pane select').on('change', function() {
        setTimeout(updateTabErrors, 100);
    });
}