#!/bin/bash
if ! [ -x "$(command -v sass)" ]; then
  echo 'Error: sass is not installed.' >&2
  exit 1
fi

if ! [ -x "$(command -v postcss)" ]; then
  echo 'Error: postcss is not installed.' >&2
  exit 1
fi

if ! [ -x "$(command -v autoprefixer)" ]; then
  echo 'Error: autoprefixer is not installed.' >&2
  exit 1
fi

FILES=(sass_processed/style.css sass_processed/content-description.css sass_processed/table.css sass_processed/ranks.css)

DARKMODE_CSS_FILES=(resources/darkmode.css resources/darkmode-svg.css)
DARKMODE_SCSS_FILES=(resources/darkmode-processed.scss resources/darkmode-svg-processed.scss)
DARKMODE_SASS_PROCESSED_FILES=(sass_processed/darkmode-processed.css sass_processed/darkmode-svg-processed.css)


# Function to convert CSS to SCSS and prepend .darkmode
convert_and_prepend_darkmode() {
  local css_file=$1
  local scss_file=$2
  local temp_file=$(mktemp)
  
  echo ".darkmode {" > $temp_file
  cat $css_file >> $temp_file
  echo "}" >> $temp_file
  
  mv $temp_file $scss_file
}

for i in "${!DARKMODE_CSS_FILES[@]}"; do
  convert_and_prepend_darkmode "${DARKMODE_CSS_FILES[$i]}" "${DARKMODE_SCSS_FILES[$i]}"
done


cd `dirname $0`
sass resources:sass_processed

echo
postcss "${FILES[@]}" --verbose --use autoprefixer -d resources

echo
postcss "${DARKMODE_SASS_PROCESSED_FILES[@]}" --verbose --use autoprefixer -d resources
