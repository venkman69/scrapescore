#!/bin/bash
SCRIPTDIR=$(readlink -f $(dirname $0))
PROJDIR=$(dirname $SCRIPTDIR)
cd $PROJDIR

where_clauses=("date_posted = '2026-03-13' AND (company LIKE '%Amazon%' or company like '%AWS%') AND title like 'Sr. Manager, Security Services SA%' AND review_status is NOT 'rejected' ")
where_clauses+=("date_posted = '2026-03-02' AND (company LIKE '%Amazon%' or company like '%AWS%') AND title like 'Sr Manager, Software Development, Amazon Foundational Security Services' AND review_status is NOT 'rejected' ")
where_clauses+=("date_posted = '2026-04-02' AND (company LIKE '%Amazon%' OR company like '%AWS%') AND title like 'Software Development Manager%' AND review_status is NOT 'rejected' ")
where_clauses+=("date_posted = '2026-02-27' AND (company LIKE '%Amazon%' OR company like '%AWS%') AND title like 'Principal Security Customer Success Specialist, AWS Specialist and Partner Organization' AND review_status is NOT 'rejected' ")

for where_clause in "${where_clauses[@]}"; do
    echo "Jobs to be updated with $where_clause"
    sqlite3 work/job_finder/job_finder.db "SELECT COUNT(*) FROM job_details WHERE $where_clause;"
    sqlite3 work/job_finder/job_finder.db "UPDATE job_details SET review_status = 'rejected' WHERE $where_clause;"
done

