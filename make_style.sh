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

cd `dirname $0`
sass resources:sass_processed

echo
postcss "${FILES[@]}" --verbose --use autoprefixer -d resources

cp sass_processed/pagedown_widget.css resources/pagedown_widget.css
cp sass_processed/dmmd-preview.css resources/dmmd-preview.css
cp resources/pagedown_widget.css resources/pagedown/demo/browser/demo.css


