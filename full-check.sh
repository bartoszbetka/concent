#!/bin/bash

printf "=================== DJANGO CONFIGURATION CHECKS ====================\n"
python3 concent_api/manage.py check
printf "\n"

printf "=============================== LINT ===============================\n"
./lint.sh
printf "\n"

printf "========================= MYPY STATIC TYPE CHECKER =================\n"
mypy --config-file=mypy.ini concent_api/
printf "\n"

printf "========================= UNIT TESTS WITH COVERAGE =================\n"
# NOTE: 'manage.py test' does not find all tests unless we run it from within the app directory.
cd concent_api/                                                                                              || exit 1
coverage run --rcfile=../coverage-config --source='.' manage.py test --settings=concent_api.settings.testing || exit 1
coverage report --show-missing                                                                               || exit 1
cd ..
