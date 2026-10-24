source .env

BASE_API="https://api.pollsapi.com/v1"
IDENTIFIER="EVG-CEDO"
POLL_ID="6adc0c0aeef92200118d7e83" # from create_poll response

YES="6adc0c0aeef92200118d7e84"
MAYBE="6adc0c0aeef92200118d7e85"
NO="6adc0c0aeef92200118d7e86"

function create_poll() {
    curl -L ${BASE_API}/create/poll -X POST -d '{
      "question": "RSVG EVG CEDO",
      "identifier": "'${IDENTIFIER}'",
      "options": [
        {
          "text": "Oui",
          "data": {}
        },
        {
          "text": "Maybe",
          "data": {}
        },
        {
          "text": "Non",
          "data": {}
        }
      ]
    }' -H "api-key: ${API_KEY}" -H "Content-Type: application/json"
}

function read_poll() {
    curl -H "api-key: ${API_KEY}" https://api.pollsapi.com/v1/get/polls-with-identifier/${IDENTIFIER} | jq .data.docs[0]
}

function vote() {
    if [[ -z "$1" ]]
    then
        echo "error: missing identifier parameter" >&2
        exit 1
    fi
    user=$1

    # shellcheck disable=SC2086
    curl -H "api-key: ${API_KEY}" -H "Content-Type: application/json" -X POST "https://api.pollsapi.com/v1/create/vote" -d '{
        "poll_id": "'${POLL_ID}'",
        "option_id": "'${MAYBE}'",
        "identifier": "'${user}'"
    }'
}

function counts() {
    curl -H "api-key: ${API_KEY}" https://api.pollsapi.com/v1/get/votes/${POLL_ID} | jq .data.docs[]
}

# vote "chocot-test"
# read_poll
counts
