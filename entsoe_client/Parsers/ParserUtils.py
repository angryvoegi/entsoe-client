"""
The XML Response format is a tree, where each node consists of meta-data and sub-nodes.
Typically, the tree decomposes as `root` -> `TimeSeries` -> `Period` -> `Point`.
A `point` is the smallest unit and holds `quantity` and `position` information.
A `period` holds meta-data on the `timeInterval` and `resolution` (=frequency) for
the contained `Points` it contains.
A `TimeSeries` is a collection of `Periods` with further metadata on the nature of a specific sub-domain of the query,
such as varying `Business Type` across TimeSeries'.
The root node holds meta-data on the global parameters of the query.

A minimal, trivial parser would purely unroll such structure recursively.
"""
import datetime
from typing import Callable, Dict, List, Any

import pandas as pd
from lxml import etree


def unfold_node(
        node: etree._Element,
) -> tuple[str, dict[str, dict[str,]]]:
    """
    Recursive unfolding of a node into a dict.
    TODO: Ensure no overwriting of same dict-names in the unfolding.
    """
    tag: str = node.tag
    children: etree._Element = node.getchildren()
    if not children:
        return tag, node.text
    else:
        return tag, dict([unfold_node(child) for child in children])


def decompose_node(node: etree._Element, subnode_tag) -> tuple[dict, list]:
    """node -> [subnodes], {metadata}"""
    if not subnode_tag:
        data_nodes: list = node.xpath(f"./*")
        data: dict = {node.tag.partition("}")[2]: dict(map(unfold_node, data_nodes))}
        return {}, [data]
    if isinstance(subnode_tag, str):
        subnodes: list = node.findall(f"./{subnode_tag}", node.nsmap)
        metadata_nodes: list = node.xpath(
            f"./*[not(self::{subnode_tag})]", namespaces=node.nsmap
        )
        metadata: dict = {node.tag: dict(map(unfold_node, metadata_nodes))}
        return metadata, subnodes
    if isinstance(subnode_tag, list):
        # Handle multiple subnode types.
        raise NotImplementedError


class Tree_to_DataFrame:
    def __init__(self, subtree_to_dataframe: Callable, subtree_tag: str):
        self.subtree_to_dataframe = subtree_to_dataframe
        self.subtree_tag = subtree_tag

    def __call__(self, root):
        metadata, subtree_list = decompose_node(root, self.subtree_tag)
        meta_dict = pd.json_normalize(metadata).iloc[0].to_dict()
        subtree_dfs = [self.subtree_to_dataframe(subtree) for subtree in subtree_list]
        df = pd.concat(subtree_dfs, axis=0)
        df = df.assign(**meta_dict)
        return df


def Root_to_DataFrame_fn() -> Callable:
    def Root_to_DataFrame(Root: etree._Element) -> pd.DataFrame:
        data = [(elem.tag, elem.text) for elem in Root.getiterator()]
        return pd.DataFrame(data, columns=['Tag', 'Value'])

    return Root_to_DataFrame


def Period_to_DataFrame_fn(get_Period_data: Callable) -> Callable:
    def Period_to_DataFrame(Period: etree._Element) -> pd.DataFrame:
        """
        Periods are implicitly valid as index is constructed independent from data extraction.
        Errors would occur at DataFrame construction.
        """
        index = get_Period_index(Period)
        data = get_Period_data(Period, length=len(index))
        assert len(data) == len(index)
        df = pd.DataFrame(data=data, index=index)

        metadata_nodes: list = Period.xpath(
            f"./*[not(self::Point)]", namespaces=Period.nsmap
        )
        metadata: dict = {Period.tag: dict(map(unfold_node, metadata_nodes))}
        meta_dict = pd.json_normalize(metadata).iloc[0].to_dict()
        df = df.assign(**meta_dict)

        return df

    return Period_to_DataFrame


def get_Period_index(Period: etree._Element) -> pd.Index:
    start = Period.timeInterval.start.text
    end = Period.timeInterval.end.text
    resolution = Period.resolution.text
    index = pd.date_range(start, end, freq=resolution_map[resolution])
    index = index[:-1] if index.size > 1 else index
    return index


# Maps response_xml resolutions to
# https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.Timedelta.html#pandas.Timedelta
resolution_map: Dict = {
    "P1Y": "12M",
    "P1M": "1M",
    "P7D": "7D",
    "P1D": "1D",
    "PT60M": "60min",
    "PT30M": "30min",
    "PT15M": "15min",
    "PT1M": "1min",
}


def check_period_data_missing(data: list[Dict[str, str]],
                              key: str) -> List[Dict]:
    """
    Checks for missing data in the data list (missing position from the api response)
    """
    data.sort(key=lambda x: int(x['position']))
    complete_data = {int(pos['position']): pos[key] for pos in data}
    for position in range(1, max(complete_data) + 1):
        if position not in complete_data:
            prev_position = max(filter(lambda x: x < position, complete_data), default=0)
            data.append({'position': str(position), key: complete_data[prev_position]})
    data.sort(key=lambda x: int(x['position']))
    return data


def get_Period_data(Period: etree._Element,
                    length: int) -> List[Dict]:
    points = Period.xpath("./Point", namespaces=Period.nsmap)
    data = [
        dict([(datum.tag, datum.text) for datum in point.iterchildren()])
        for point in points
    ]
    key = list(data[0].keys())[1]
    data = check_period_data_missing(data=data,
                                     key=key)
    # check for missing positions if the length of the index is longer that the data (and no position is missing
    # inside the data list)
    if length != len(data):
        data += [{'position': str(i + 1), key: data[-1][key]} for i in range(length - len(data))]
    return data


def get_Period_Financial_Price_data(Period: etree._Element) -> List[Dict]:
    """
    TODO: Could be abstracted into `get_Period_data.
    """
    points = Period.xpath("./Point", namespaces=Period.nsmap)
    data = [get_Point_Financial_Price_data(point) for point in points]
    return data


def get_Point_Financial_Price_data(Point: etree._Element) -> Dict:
    """
    If a `Point` has overlapping `Financial_Price` field,
    the standard Procedure does not work.

    Applicable at e.g. FinancialExpensesAndIncomeForBalancing
    """
    direction_map = {"A01": "up", "A02": "down", "A03": "up_and_down"}

    datum = {Point.position.tag: Point.position.text}
    for fp in Point.Financial_Price:
        datum[
            ".".join([fp.tag, direction_map[fp.direction.text], fp.amount.tag])
        ] = fp.amount.text

    return datum


def get_index(Period: etree._Element) -> pd.Index:
    """
    Get index of the series
    """
    points = Period.xpath("unavailability_Time_Period.timeInterval")
    data = [
        dict([(datum.tag, datum.text) for datum in point.iterchildren()])
        for point in points
    ][0]
    start = data['start']
    end = data['end']
    resolution = Period.xpath("./TimeSeries/Available_Period/resolution")
    if not resolution:
        index = [pd.to_datetime(datetime.datetime.now().replace(minute=0, second=0, microsecond=0), )]
    else:
        index = pd.date_range(start, end, freq=resolution_map[resolution[0]])
        index = index[:-1] if index.size > 1 else index
    return index


def get_data(Period: etree._Element) -> tuple[pd.DatetimeIndex, list[dict[Any, Any]]]:
    """
    Get the data of the series
    """
    index = get_index(Period=Period)
    points = Period.xpath("./TimeSeries/Available_Period/Point")
    data = [
        dict([(datum.tag, datum.text) for datum in point.iterchildren()])
        for point in points
    ]
    if not data:
        data = [{'position': -1,
                'quantity': 0}]
        return index, data
    else:
        new_ind = []
        for elem in data:
            new_ind.append(index[int(elem['position']) - 1])
        new_ind = pd.DatetimeIndex(new_ind)
        return new_ind, data


def get_resource(Period: etree._Element) -> Dict:
    """
    Handle the case when there are multiple affected assets
    :param Period:
    :return:
    """
    tag_available = Period.find("./TimeSeries/Asset_RegisteredResource")
    if tag_available is not None:
        points = Period.xpath("./TimeSeries/Asset_RegisteredResource")
        data = [
            dict([(f"Asset_RegisteredResource.{datum.tag}", datum.text) for datum in point.iterchildren()])
            for point in points
        ]
        if len(data) == 0:
            data = [{'Asset_RegisteredResource.mRID': '',
                     'Asset_RegisteredResource.name': '',
                     'Asset_RegisteredResource.asset_PSRType.psrType': '',
                     'Asset_RegisteredResource.location.name': ''
                     }]
        if all(isinstance(entry, dict) for entry in data):
            result = {}
            for entry in data:
                for key, value in entry.items():
                    result.setdefault(key, []).append(value)
            result = {key: ', '.join(values) for key, values in result.items()}
        return result


def get_infos(Period: etree._Element) -> Dict:
    """
    Get additional infos of the document for the series
    """
    points = Period.xpath("//TimeSeries", namespaces=Period.nsmap)
    data = [
        dict([(f"TimeSeries.{datum.tag}", datum.text) for datum in point.iterchildren()])
        for point in points
    ][0]
    data.pop('TimeSeries.Asset_RegisteredResource', None)
    data.pop('TimeSeries.Available_Period', None)
    data.pop('TimeSeries.Reason', None)
    return data


def get_reason(Period: etree._Element) -> Dict:
    """
    Get the reason for the outage
    """
    points = Period.xpath("./Reason", namespaces=Period.nsmap)
    data = [
        dict([(f"Reason.{datum.tag}", datum.text) for datum in point.iterchildren()])
        for point in points
    ][0]
    if 'Reason.text' not in data:
        data['Reason.text'] = ''
    else:
        if data['Reason.text'] == '\n    ':
            data['Reason.text'] = ''
    return data


def get_createdatetime(Period: etree._Element) -> Dict:
    """
    Get the creation Date Time
    """
    created = Period.xpath("./createdDateTime", namespaces=Period.nsmap)
    docstat = Period.xpath("./docStatus/value", namespaces=Period.nsmap)
    revision = Period.xpath("./revisionNumber", namespaces=Period.nsmap)
    resolution = Period.xpath("//TimeSeries/Available_Period/resolution", namespaces=Period.nsmap)
    data = {'CreatedDateTime': str(created[0]) if len(created) == 1 else None,
            'DocStatus': str(docstat[0]) if len(docstat) == 1 else None,
            'RevisionNumber': str(revision[0]) if len(revision) == 1 else None,
            'Resolution': str(resolution_map[resolution[0]]) if len(resolution) == 1 else None}
    return data


def outage_transmission() -> Callable:
    def outage_dataframe(Period: etree._Element) -> pd.DataFrame:
        """
        Build the dataframe from the series
        """
        index, data = get_data(Period=Period)
        assert len(data) == len(index)
        df = pd.DataFrame(data=data, index=index)
        resource = get_resource(Period=Period)
        infos = get_infos(Period=Period)
        reason = get_reason(Period=Period)
        created = get_createdatetime(Period=Period)
        if resource is not None:
            final = df.assign(**{**resource, **infos, **reason, **created})
        else:
            final = df.assign(**{**infos, **reason, **created})
        return final
    return outage_dataframe


StandardOutagesTransmissionParser = outage_transmission()
StandardPeriodParser = Period_to_DataFrame_fn(get_Period_data)
StandardErrorDocumentParser = Root_to_DataFrame_fn()
