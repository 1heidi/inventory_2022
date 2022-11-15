#!/usr/bin/env python3
"""
Purpose: Get metadata from EuropePMC query
Authors: Kenneth Schackart
"""

import argparse
import os
import re
from collections import defaultdict
from typing import NamedTuple, Optional, TextIO, Tuple, cast

import numpy as np
import pandas as pd
import pycountry
import requests
from pandas.testing import assert_frame_equal, assert_series_equal

from inventory_utils.custom_classes import CustomHelpFormatter
from inventory_utils.wrangling import chunk_rows, join_commas


# ---------------------------------------------------------------------------
class Args(NamedTuple):
    """ Command-line arguments """
    file: TextIO
    out_dir: str
    chunk_size: Optional[int]


# ---------------------------------------------------------------------------
def get_args() -> Args:
    """ Parse command-line arguments """

    parser = argparse.ArgumentParser(
        description=('Get metadata from EuropePMC query'),
        formatter_class=CustomHelpFormatter)

    parser.add_argument('file',
                        metavar='FILE',
                        type=argparse.FileType('rt', encoding='ISO-8859-1'),
                        help='CSV File with ID column for articles')
    parser.add_argument('-o',
                        '--out-dir',
                        metavar='DIR',
                        type=str,
                        default='out/',
                        help='Output directory')
    parser.add_argument('-s',
                        '--chunk-size',
                        metavar='INT',
                        type=int,
                        help=('Number of rows '
                              'to process at a time.'))

    args = parser.parse_args()

    return Args(args.file, args.out_dir, args.chunk_size)


# ---------------------------------------------------------------------------
def separate_ids(df: pd.DataFrame) -> pd.DataFrame:
    """
    Separate IDs into one row per ID. Assign a resource number to each row
    first so that they can be remerged after querying EuropePMC.
    
    Parameters:
    `df`: Deduplicated dataframe
    
    Return: Dataframe with one row per ID
    """

    df['resource_num'] = np.arange(len(df))

    df['ID'] = df['ID'].str.split(', ')

    df = df.explode('ID')

    df.reset_index(drop=True, inplace=True)

    return df


# ---------------------------------------------------------------------------
def test_separate_ids() -> None:
    """ Test separate_ids() """

    in_df = pd.DataFrame(
        [['123', 'text1', 'url1'], ['456, 789', 'text2', 'url2'],
         ['147', 'text3', 'url3']],
        columns=['ID', 'text', 'extracted_url'])

    out_df = pd.DataFrame(
        [['123', 'text1', 'url1', 0], ['456', 'text2', 'url2', 1],
         ['789', 'text2', 'url2', 1], ['147', 'text3', 'url3', 2]],
        columns=['ID', 'text', 'extracted_url', 'resource_num'])

    assert_frame_equal(separate_ids(in_df), out_df)


# ---------------------------------------------------------------------------
def make_filenames(outdir: str) -> Tuple[str, str]:
    '''
    Make filenames for output csv file and last date text file

    Parameters:
    `outdir`: Output directory

    Return: Tuple of csv and txt filenames
    '''

    csv_out = os.path.join(outdir, 'query_results.csv')
    txt_out = os.path.join(outdir, 'last_query_date.txt')

    return csv_out, txt_out


# ---------------------------------------------------------------------------
def test_make_filenames() -> None:
    """ Test make_filenames() """

    assert make_filenames('data/new_query') == (
        'data/new_query/query_results.csv',
        'data/new_query/last_query_date.txt')


# ---------------------------------------------------------------------------
def clean_results(results: dict) -> pd.DataFrame:
    """
    Retrieve the metadata from results of query

    Parameters:
    `results`: JSON-encoded response (nested dictionary)

    Return: Dataframe of results
    """

    parsed_info = defaultdict(list)
    for paper in results.get('resultList').get('result'):  # type: ignore
        parsed_info['ids'].append(paper.get('id'))
        parsed_info['titles'].append(paper.get('title'))
        parsed_info['abstracts'].append(paper.get('abstractText'))
        parsed_info['affiliations'].append(paper.get('affiliation'))

        authors = []
        for author in paper.get('authorList', {}).get('author', {}):
            if author:
                authors.append(author.get('fullName', ''))
            else:
                authors.append('')
        parsed_info['authors'].append(', '.join(authors))

        grant_ids = []
        agencies = []
        for grant in paper.get('grantsList', {}).get('grant', {}):
            if grant:
                grant_ids.append(grant.get('grantID', ''))
                agencies.append(grant.get('agency', ''))
            else:
                grant_ids.append('')
                agencies.append('')
        parsed_info['grant_ids'].append(', '.join(
            [grant_id for grant_id in grant_ids if grant_id]))
        parsed_info['agencies'].append(', '.join(
            [agency for agency in agencies if agency]))

    return pd.DataFrame({
        'ID': parsed_info['ids'],
        'affiliation': parsed_info['affiliations'],
        'authors': parsed_info['authors'],
        'grant_ids': parsed_info['grant_ids'],
        'grant_agencies': parsed_info['agencies']
    })


# ---------------------------------------------------------------------------
def run_query(ids: pd.Series, chunk_size: Optional[int]) -> pd.DataFrame:
    """
    Run query on EuropePMC API

    Parameters:
    `ids`: Dataframe ID column
    `chunk_size`: Maximum number of IDs to check per request

    Return: `DataFrame` of returned article information
    """

    out_df = pd.DataFrame()

    for id_chunk in chunk_rows(ids, chunk_size):
        query = ' OR '.join(set(id_chunk))
        prefix = ('https://www.ebi.ac.uk/europepmc/'
                  'webservices/rest/search?query=')
        suffix = '&resultType=core&fromSearchPost=false&format=json'
        url = prefix + query + suffix

        # Not using try-except because if there is an exception,
        # it means the API has changed.
        results = requests.get(url)
        status = results.status_code
        if status != requests.codes.ok:  # pylint: disable=no-member
            results.raise_for_status()

        results_json = cast(dict, results.json())

        cleaned_results = clean_results(results_json)

        pd.concat([out_df, cleaned_results])

    return out_df


# ---------------------------------------------------------------------------
def extract_countries(affiliations: pd.Series) -> pd.Series:
    """
    Extract country names from affiliations column

    Parameters:
    `affiliations`: Column of affiliations

    Return: column of extracted country names
    """

    countries = []
    for affiliation in affiliations:
        found_countries = []
        for country in pycountry.countries:
            if any(
                    re.search(fr'\b{x}\b', affiliation)
                    for x in [country.name, country.alpha_3, country.alpha_2]):
                found_countries.append(country.name)
        countries.append(', '.join(found_countries))

    return pd.Series(countries)


# ---------------------------------------------------------------------------
def test_extract_countries() -> None:
    """ Test extract_countries() """

    in_col = pd.Series([
        'USA.', 'United States', 'US', 'The United States of America',
        '605014, India.', 'France', 'UK'
    ])

    out_col = pd.Series([
        'United States', 'United States', 'United States', 'United States',
        'India', 'France', 'UK'
    ])

    assert_series_equal(extract_countries(in_col), out_col)


# ---------------------------------------------------------------------------
def remerge_resources(df: pd.DataFrame) -> pd.DataFrame:
    """
    Re-merge rows that have the same resource number
    (arbitrarily assigned while separating IDs).

    Parameters:
    `df`: input dataframe with one row per ID

    Return: dataframe with one row per resource
    """

    df.groupby('resource_num').agg({
        'ID': join_commas,
        'best_name': 'first',
        'best_name_prob': 'first',
        'best_common': 'first',
        'best_common_prob': 'first',
        'best_full': 'first',
        'best_full_prob': 'first',
        'article_count': 'first',
        'extracted_url': 'first',
        'extracted_url_status': 'first',
        'extracted_url_country': 'first',
        'extracted_url_coordinates': 'first',
        'wayback_url': 'first',
        'publication_date': 'first',
        'affiliation': join_commas  #,
        #'countries': join_commas
    }).reset_index

    df.drop('resource_num', axis='columns', inplace=True)

    return df


# ---------------------------------------------------------------------------
def main() -> None:
    """ Main function """

    args = get_args()
    out_dir = args.out_dir

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    df = pd.read_csv(args.file, dtype=str)

    df = separate_ids(df)

    results = run_query(df['ID'], args.chunk_size)
    results['ID'] = results['ID'].astype(str)

    all_info = pd.merge(df, results, how='inner', on='ID')

    # all_info['countries'] = extract_countries(all_info['affiliation'])

    out_df = remerge_resources(all_info)

    out_file = os.path.join(out_dir, os.path.basename(args.file.name))

    out_df.to_csv(out_file, index=False)

    print(f'Done. Wrote output to {out_file}.')


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    main()
