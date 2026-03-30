"""
CCC — University Detector

Maps .ac.uk and .edu email domains to human-readable university names.
Used in onboarding to pre-fill the university field from the student's email.
"""

# UK universities: domain suffix → display name
UK_UNIVERSITIES: dict[str, str] = {
    "ucl.ac.uk":              "University College London",
    "lse.ac.uk":              "London School of Economics",
    "imperial.ac.uk":         "Imperial College London",
    "kcl.ac.uk":              "King's College London",
    "ox.ac.uk":               "University of Oxford",
    "cam.ac.uk":              "University of Cambridge",
    "ed.ac.uk":               "University of Edinburgh",
    "manchester.ac.uk":       "University of Manchester",
    "bristol.ac.uk":          "University of Bristol",
    "warwick.ac.uk":          "University of Warwick",
    "dur.ac.uk":              "Durham University",
    "bath.ac.uk":             "University of Bath",
    "exeter.ac.uk":           "University of Exeter",
    "nottingham.ac.uk":       "University of Nottingham",
    "birmingham.ac.uk":       "University of Birmingham",
    "leeds.ac.uk":            "University of Leeds",
    "sheffield.ac.uk":        "University of Sheffield",
    "st-andrews.ac.uk":       "University of St Andrews",
    "soton.ac.uk":            "University of Southampton",
    "lancaster.ac.uk":        "Lancaster University",
    "qmul.ac.uk":             "Queen Mary University of London",
    "rhul.ac.uk":             "Royal Holloway, University of London",
    "york.ac.uk":             "University of York",
    "sussex.ac.uk":           "University of Sussex",
    "reading.ac.uk":          "University of Reading",
    "liverpool.ac.uk":        "University of Liverpool",
    "Cardiff.ac.uk":          "Cardiff University",
    "abdn.ac.uk":             "University of Aberdeen",
    "gla.ac.uk":              "University of Glasgow",
    "strath.ac.uk":           "University of Strathclyde",
    "city.ac.uk":             "City, University of London",
    "brunel.ac.uk":           "Brunel University London",
    "aston.ac.uk":            "Aston University",
    "surrey.ac.uk":           "University of Surrey",
    "leicester.ac.uk":        "University of Leicester",
    "uea.ac.uk":              "University of East Anglia",
    "hull.ac.uk":             "University of Hull",
    "keele.ac.uk":            "Keele University",
    "essex.ac.uk":            "University of Essex",
    "lboro.ac.uk":            "Loughborough University",
    "ncl.ac.uk":              "Newcastle University",
    "kent.ac.uk":             "University of Kent",
    "qub.ac.uk":              "Queen's University Belfast",
    "hw.ac.uk":               "Heriot-Watt University",
    "napier.ac.uk":           "Edinburgh Napier University",
    "dundee.ac.uk":           "University of Dundee",
    "stir.ac.uk":             "University of Stirling",
    "smu.ac.uk":              "Saint Mary's University",
    "coventry.ac.uk":         "Coventry University",
    "dmu.ac.uk":              "De Montfort University",
    "uel.ac.uk":              "University of East London",
    "mdx.ac.uk":              "Middlesex University",
    "westminster.ac.uk":      "University of Westminster",
    "roehampton.ac.uk":       "University of Roehampton",
    "plymouth.ac.uk":         "University of Plymouth",
    "uwe.ac.uk":              "UWE Bristol",
    "arts.ac.uk":             "University of the Arts London",
    "goldsmiths.ac.uk":       "Goldsmiths, University of London",
    "bbk.ac.uk":              "Birkbeck, University of London",
    "soas.ac.uk":             "SOAS University of London",
    "lshtm.ac.uk":            "London School of Hygiene & Tropical Medicine",
    "cranfield.ac.uk":        "Cranfield University",
    "bournemouth.ac.uk":      "Bournemouth University",
    "herts.ac.uk":            "University of Hertfordshire",
    "anglia.ac.uk":           "Anglia Ruskin University",
    "lincoln.ac.uk":          "University of Lincoln",
    "staffs.ac.uk":           "Staffordshire University",
    "glyndwr.ac.uk":          "Wrexham Glyndwr University",
    "bangor.ac.uk":           "Bangor University",
    "aber.ac.uk":             "Aberystwyth University",
    "swansea.ac.uk":          "Swansea University",
}

# US universities: subdomain pattern → display name (e.g. student@college.harvard.edu)
US_UNIVERSITIES: dict[str, str] = {
    "harvard.edu":            "Harvard University",
    "yale.edu":               "Yale University",
    "princeton.edu":          "Princeton University",
    "columbia.edu":           "Columbia University",
    "upenn.edu":              "University of Pennsylvania",
    "dartmouth.edu":          "Dartmouth College",
    "brown.edu":              "Brown University",
    "cornell.edu":            "Cornell University",
    "mit.edu":                "Massachusetts Institute of Technology",
    "stanford.edu":           "Stanford University",
    "caltech.edu":            "California Institute of Technology",
    "uchicago.edu":           "University of Chicago",
    "duke.edu":               "Duke University",
    "vanderbilt.edu":         "Vanderbilt University",
    "rice.edu":               "Rice University",
    "wustl.edu":              "Washington University in St. Louis",
    "emory.edu":              "Emory University",
    "georgetown.edu":         "Georgetown University",
    "notre-dame.edu":         "University of Notre Dame",
    "nd.edu":                 "University of Notre Dame",
    "tufts.edu":              "Tufts University",
    "tulane.edu":             "Tulane University",
    "northwestern.edu":       "Northwestern University",
    "georgetown.edu":         "Georgetown University",
    "nyu.edu":                "New York University",
    "bu.edu":                 "Boston University",
    "bc.edu":                 "Boston College",
    "northeastern.edu":       "Northeastern University",
    "usc.edu":                "University of Southern California",
    "ucla.edu":               "UCLA",
    "ucsd.edu":               "UC San Diego",
    "ucsb.edu":               "UC Santa Barbara",
    "uci.edu":                "UC Irvine",
    "berkeley.edu":           "UC Berkeley",
    "umich.edu":              "University of Michigan",
    "illinois.edu":           "University of Illinois",
    "wisc.edu":               "University of Wisconsin–Madison",
    "umn.edu":                "University of Minnesota",
    "purdue.edu":             "Purdue University",
    "ohio-state.edu":         "Ohio State University",
    "osu.edu":                "Ohio State University",
    "psu.edu":                "Penn State University",
    "unc.edu":                "University of North Carolina",
    "virginia.edu":           "University of Virginia",
    "gatech.edu":             "Georgia Tech",
    "cmu.edu":                "Carnegie Mellon University",
    "utexas.edu":             "University of Texas at Austin",
    "uw.edu":                 "University of Washington",
    "nyu.edu":                "New York University",
    "fordham.edu":            "Fordham University",
    "gwu.edu":                "George Washington University",
    "american.edu":           "American University",
}


def detect_university(email: str) -> str:
    """
    Return human-readable university name from a student email address.
    Returns empty string if unrecognised.
    """
    email = email.lower().strip()
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1]

    # Direct match
    all_unis = {**UK_UNIVERSITIES, **US_UNIVERSITIES}
    if domain in all_unis:
        return all_unis[domain]

    # UK: try stripping subdomain (e.g. stu.ucl.ac.uk → ucl.ac.uk)
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in all_unis:
            return all_unis[candidate]

    # Generic .ac.uk fallback — capitalise the domain segment
    if domain.endswith(".ac.uk"):
        # e.g. "myuni.ac.uk" → "Myuni"
        name_part = domain.replace(".ac.uk", "").split(".")[-1]
        return name_part.replace("-", " ").title() + " University"

    return ""


# Expose as a simple API endpoint payload
def university_from_email_response(email: str) -> dict:
    name = detect_university(email)
    return {
        "email":      email,
        "university": name,
        "detected":   bool(name),
    }
